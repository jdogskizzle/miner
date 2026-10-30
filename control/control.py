"""Control panel for the miner: a small web page that shows what the miner is doing and
lets you pause it, pin a coin, pick a power mode, benchmark a coin, or ask the switcher to
compare prices now.

It never touches Docker or the GPU itself. Choices go into /state/control.json, which
gpu-yield (pause, benchmarks) and the switcher (pin, power mode, check now) read; status
comes from the state files they write and from the exporter's metrics."""
import json
import os
import re
import threading
import time
import tomllib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def required(name):
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"{name} is not set")
    return val


STATE = required("STATE_DIR")
COINS_FILE = required("COINS_FILE")
EXPORTER_URL = required("EXPORTER_URL")
PORT = int(os.environ.get("PORT", "8000"))
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
LIVE_METRICS = {"gpu_power_watts", "gpu_power_limit_watts", "gpu_temperature_celsius", "gpu_fan_percent",
                "miner_up", "miner_hashrate_hs", "miner_shares_accepted", "miner_shares_rejected",
                "miner_uptime_seconds", "miner_est_usd_per_day"}
MODES = ("max", "efficient", "low")
lock = threading.Lock()


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


def status():
    cfg = coins_config()
    return {
        "now": time.time(),
        "watchdog": read_json("status.json", None),
        "control": read_json("control.json", {}),
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
    {"power_mode": "max" | "efficient" | "low"} / {"benchmark": "PRL"} / {"benchmark_cancel": true}."""
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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self.send(200, status())
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/api/control":
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
    print(time.strftime("%F %T"), f"control panel on :{PORT}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
