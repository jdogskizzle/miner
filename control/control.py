"""Control panel for the miner: a small web page that shows what the miner is doing and
lets you pause it, pin a coin, pick a power mode, benchmark a coin, or ask the switcher to
compare prices now.

It never touches Docker or the GPU itself. Choices go into /state/control.json, which
gpu-yield (pause, benchmarks) and the switcher (pin, power mode, check now) read; status
comes from the state files they write and from the exporter's metrics.

Everything except the sign-in page needs a signed-in session (formlogin)."""
import json
import os
import re
import threading
import time
import tomllib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import formlogin
import formlogin.httpserver


def required(name):
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"{name} is not set")
    return val


STATE = required("STATE_DIR")
COINS_FILE = required("COINS_FILE")
EXPORTER_URL = required("EXPORTER_URL")
PORT = int(os.environ.get("PORT", "8000"))
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")
LIVE_METRICS = {"gpu_power_watts", "gpu_power_limit_watts", "gpu_temperature_celsius", "gpu_fan_percent",
                "miner_up", "miner_hashrate_hs", "miner_shares_accepted", "miner_shares_rejected",
                "miner_uptime_seconds", "miner_est_usd_per_day"}
MODES = ("max", "efficient", "low")
POWER_FILE = "power.json"   # energy used while mining, per hour (written here)
SAMPLE_SECONDS = 30
KEEP_DAYS = 400
WINDOWS = (("day", 86400), ("week", 7 * 86400), ("month", 30 * 86400), ("all", None))
lock = threading.Lock()


def env_rate():
    """Default electricity price in $/kWh (POWER_USD_PER_KWH); the panel can override it."""
    val = os.environ.get("POWER_USD_PER_KWH")
    if not val:
        return None
    try:
        rate = float(val)
    except ValueError:
        raise SystemExit(f"POWER_USD_PER_KWH must be a price in $/kWh, e.g. 0.13 (got {val!r})")
    if not 0 <= rate <= 5:
        raise SystemExit(f"POWER_USD_PER_KWH looks wrong: {rate} $/kWh")
    return rate


ENV_RATE = env_rate()


def read_json(name, default):
    try:
        with open(os.path.join(STATE, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(name, data):
    path = os.path.join(STATE, name)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".tmp", path)


def coins_config():
    with open(COINS_FILE, "rb") as f:
        return tomllib.load(f)


def live():
    """A few numbers from the exporter (GPU power/temperature, hashrate, shares)."""
    try:
        with urllib.request.urlopen(EXPORTER_URL, timeout=3) as r:
            text = r.read().decode()
    except Exception:
        return None
    out = {}
    for m in re.finditer(r"^([a-z_]+) ([-\d.e+]+)$", text, re.M):
        if m.group(1) in LIVE_METRICS:
            out[m.group(1)] = float(m.group(2))
    return out


def sample_power():
    """Every SAMPLE_SECONDS, add the GPU's energy (and the estimated earnings) to the
    current hour's totals while the miner is running."""
    last = None
    while True:
        now = time.time()
        dt = min(now - last, 2 * SAMPLE_SECONDS) if last else SAMPLE_SECONDS
        last = now
        m = live() or {}
        watts = m.get("gpu_power_watts")
        if m.get("miner_up") and watts is not None:
            usd_day = m.get("miner_est_usd_per_day") or 0
            if (read_json("status.json", None) or {}).get("state") == "benchmarking":
                # the exporter prices the active coin, not the one being benchmarked
                coin = (read_json("benchmark.json", None) or {}).get("coin")
                usd_day = ((read_json("estimates.json", None) or {}).get("coins", {}).get(coin) or {}).get("usd_day") or 0
            with lock:
                led = read_json(POWER_FILE, {})
                hours = led.setdefault("hours", {})
                h = str(int(now // 3600 * 3600))
                secs, kwh, usd = hours.get(h, (0, 0, 0))
                hours[h] = [round(secs + dt, 1), round(kwh + watts * dt / 3.6e6, 6), round(usd + usd_day * dt / 86400, 6)]
                cutoff = now - KEEP_DAYS * 86400
                for k in [k for k in hours if float(k) < cutoff]:
                    del hours[k]
                led.setdefault("since", now)
                write_json(POWER_FILE, led)
        time.sleep(SAMPLE_SECONDS)


def power_summary(ctl):
    led = read_json(POWER_FILE, {})
    now = time.time()
    windows = {}
    for name, span in WINDOWS:
        secs = kwh = usd = 0
        for h, (s, k, u) in led.get("hours", {}).items():
            if span is None or float(h) + 3600 > now - span:
                secs, kwh, usd = secs + s, kwh + k, usd + u
        windows[name] = {"mining_s": secs, "kwh": kwh, "earned": usd}
    panel = ctl.get("power_price")
    return {"rate": panel if panel is not None else ENV_RATE,
            "rate_source": "panel" if panel is not None else ".env" if ENV_RATE is not None else None,
            "env_rate": ENV_RATE, "since": led.get("since"), "windows": windows}


def status():
    cfg = coins_config()
    ctl = read_json("control.json", {})
    return {
        "now": time.time(),
        "watchdog": read_json("status.json", None),
        "control": ctl,
        "power": power_summary(ctl),
        "active": read_json("active.json", None),
        "estimates": read_json("estimates.json", None),
        "tuning": read_json("tuning.json", {}),
        "benchmark": read_json("benchmark.json", None),
        "config_power_mode": cfg["settings"].get("power_mode", "efficient"),
        "coins": [{"key": k, "name": c["name"], "algorithm": c["algorithm"]} for k, c in cfg["coins"].items()],
        "config_pin": cfg["settings"].get("pin") or "",
        "check_minutes": cfg["settings"].get("check_minutes"),
        "live": live(),
    }


def update_control(req):
    """Apply {"paused": bool} / {"pin": "PRL" or ""} / {"check": true} /
    {"power_mode": "max" | "efficient" | "low"} / {"benchmark": "PRL"} / {"benchmark_cancel": true} /
    {"power_price": 0.13 ($/kWh) or null for the .env default}."""
    with lock:
        ctl = read_json("control.json", {})
        if "paused" in req:
            if not isinstance(req["paused"], bool):
                raise ValueError("paused must be true or false")
            ctl["paused"] = req["paused"]
        if "pin" in req:
            pin = req["pin"] or ""
            if pin and pin not in coins_config()["coins"]:
                raise ValueError(f"unknown coin {pin!r}")
            ctl["pin"] = pin
        if req.get("check"):
            ctl["check_requested"] = time.time()
        if "power_mode" in req:
            if req["power_mode"] not in MODES:
                raise ValueError(f"power_mode must be one of {', '.join(MODES)}")
            ctl["power_mode"] = req["power_mode"]
        if "benchmark" in req:
            if req["benchmark"] not in coins_config()["coins"]:
                raise ValueError(f"unknown coin {req['benchmark']!r}")
            ctl["benchmark"] = {"coin": req["benchmark"], "requested": time.time()}
        if req.get("benchmark_cancel"):
            ctl["benchmark_cancel"] = time.time()
            ctl.pop("benchmark", None)
        if "power_price" in req:
            rate = req["power_price"]
            if rate is None:
                ctl.pop("power_price", None)
            elif isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 5:
                raise ValueError("power_price must be a price in $/kWh between 0 and 5")
            else:
                ctl["power_price"] = rate
        write_json("control.json", ctl)
        return ctl


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in formlogin.SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if formlogin.httpserver.handle(self, LOGIN, path):
            return
        if path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self.send(200, status())
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if formlogin.httpserver.handle(self, LOGIN, path):
            return
        if path != "/api/control":
            return self.send(404, {"error": "not found"})
        # JSON only: a plain cross-site form post can't set this content type.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(415, {"error": "send JSON"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if not isinstance(req, dict):
                raise ValueError("expected an object")
            ctl = update_control(req)
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        print(time.strftime("%F %T"), "control:", json.dumps(req), flush=True)
        self.send(200, ctl)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    coins_config()  # fail now if coins.toml is missing or broken
    LOGIN = formlogin.Login.from_env(  # password: python -m formlogin passwd (see README)
        "CONTROL", "miner", STATE, title="Miner",
        icon="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><circle cx='8' cy='8' r='6' fill='%2322c55e'/></svg>")
    threading.Thread(target=sample_power, daemon=True).start()
    print(time.strftime("%F %T"), f"control panel on :{PORT}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
