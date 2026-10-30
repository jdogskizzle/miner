"""Coin switcher: every few minutes, estimates what each coin in coins.toml would earn on
this GPU and switches the miner to a better one when it's clearly and consistently ahead.

It only decides *which coin*. The active choice is written to /state/active.env (sourced
by the miner and gpu-yield) and /state/active.json (read by the exporter). When the coin
changes and the miner is running, it's restarted; if gpu-yield has it paused, the next
start simply uses the new coin."""
import html
import http.client
import json
import os
import re
import shlex
import socket
import threading
import time
import tomllib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = os.environ.get("COINS_FILE", "/config/coins.toml")
STATE = os.environ.get("STATE_DIR", "/state")
MINER = os.environ.get("MINER_CONTAINER", "miner")
MINER_API = os.environ.get("MINER_API", "http://miner:21550")
GPU_MODEL = os.environ.get("GPU_MODEL", "3080ti")  # hashrate.no GPU page, e.g. 3080ti, 4090, 7900xtx
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) home-server coin switcher"}
UNITS = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}

metrics = {"estimates": {}, "rev": {}, "hashrate": {}, "source": {}, "price": {}, "btc": None,
           "switches": 0, "last_check": 0, "ok": 0}


def log(msg):
    print(time.strftime("%F %T"), msg, flush=True)


# ------------------------------------------------------------------ state files

def read_json(name, default):
    try:
        with open(os.path.join(STATE, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_atomic(name, text):
    path = os.path.join(STATE, name)
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def pool_address(coin):
    """Pool address with the configured region filled in ("prl{region}.kryptex.network:7048")."""
    region = CFG["settings"].get("pool_region", "")
    return coin["pool"].replace("{region}", f"-{region}" if region else "")


def write_profile(key, coin, rev, since):
    prof = {"coin": key, "name": coin["name"], "algorithm": coin["algorithm"], "pool": pool_address(coin),
            "power_limit": coin.get("power_limit", 250), "core_clock": coin.get("core_clock", ""),
            "mem_clock": coin.get("mem_clock", ""), "rev_per_hs_day": rev,
            "fee_factor": (1 - coin.get("dev_fee", 0)) * (1 - CFG["settings"].get("pool_fee", 0)),
            "price_usd": metrics["price"].get(key), "since": since}
    write_atomic("active.json", json.dumps(prof, indent=1))
    env = {"PROFILE_COIN": key, "ALGORITHM": coin["algorithm"], "POOL": prof["pool"],
           "PROFILE_POWER_LIMIT": prof["power_limit"], "PROFILE_CORE_CLOCK": prof["core_clock"],
           "PROFILE_MEM_CLOCK": prof["mem_clock"]}
    write_atomic("active.env", "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env.items()))
    return prof


# ------------------------------------------------------------------ docker

class UnixConn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=30)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/docker.sock")


def docker(method, path):
    c = UnixConn()
    c.request(method, path)
    r = c.getresponse()
    body = r.read()
    return r.status, (json.loads(body) if body and body[:1] in b"{[" else body)


def miner_running():
    status, info = docker("GET", f"/containers/{MINER}/json")
    return status == 200 and info["State"]["Running"]


# ------------------------------------------------------------------ market data

def fetch(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def btc_usd():
    return json.loads(fetch("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"))["bitcoin"]["usd"]


def whattomine():
    """{id: (usd per H/s per day, usd volume per day, usd price)}"""
    data = json.loads(fetch("https://whattomine.com/coins.json"))["coins"]
    out = {}
    btc = btc_usd()
    metrics["btc"] = btc
    for c in data.values():
        try:
            if c.get("lagging"):
                continue
            coins_per_hs = float(c["block_reward"]) * 86400 / float(c["block_time"]) / float(c["nethash"])
            out[c["id"]] = (coins_per_hs * float(c["exchange_rate"]) * btc,
                            float(c.get("exchange_rate_vol") or 0) * btc,
                            float(c["exchange_rate"]) * btc)
        except (KeyError, ValueError, ZeroDivisionError, TypeError):
            continue
    return out


def hashrate_no():
    """{ticker: (usd per H/s per day, usd volume per day, benchmark H/s)} from hashrate.no's
    estimates page for this GPU model."""
    t = html.unescape(re.sub(r"<script.*?</script>|<style.*?</style>", "",
                             fetch(f"https://www.hashrate.no/gpus/{GPU_MODEL}"), flags=re.S))
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))
    vol = {m.group(4): float(m.group(1).replace(",", ""))
           for m in re.finditer(r"Volume \$([\d,]+) ([\d.]+) ([KMGTPE]?)h/s Hashrate (\S+)", t)}
    out = {}
    for m in re.finditer(r"Hashrate (\S+) ([\d.]+) ([KMGTPE]?)h/s Power \d+ w .*? Revenue \$([\d.]+) ", t):
        tag, hs, unit, rev = m.group(1), float(m.group(2)), m.group(3), float(m.group(4))
        if hs > 0:
            out[tag] = (rev / (hs * UNITS[unit]), vol.get(tag), hs * UNITS[unit])
    return out


def hashrate_no_price(ticker):
    """Coin price from its hashrate.no page (for coins WhatToMine doesn't list)."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", fetch(f"https://www.hashrate.no/coins/{ticker}")))
    m = re.search(r"Price\s+Up to date\s+\$([\d.]+)", re.sub(r"\s+", " ", t))
    return float(m.group(1)) if m else None


# ------------------------------------------------------------------ measured hashrates

def learn_hashrate(active):
    """Record the miner's real hashrate for the active coin once it has run 20+ minutes."""
    try:
        with urllib.request.urlopen(MINER_API, timeout=5) as r:
            d = json.load(r)
        a = d["algorithms"][0]
        if a["name"] != active["algorithm"] or (a["pool"].get("uptime") or 0) < 1200:
            return
        hs = float(a["hashrate"].get("15min") or 0)
        if hs <= 0:
            return
        known = read_json("hashrates.json", {})
        old = known.get(active["coin"])
        known[active["coin"]] = round(hs if not old else old * 0.7 + hs * 0.3, 2)  # smooth
        write_atomic("hashrates.json", json.dumps(known, indent=1))
    except Exception:
        pass  # miner paused or not answering


# ------------------------------------------------------------------ decision

def estimate():
    s = CFG["settings"]
    try:
        wtm = whattomine()
    except Exception as e:
        log(f"WhatToMine unavailable: {e}")
        wtm = {}
    try:
        hrn = hashrate_no()
    except Exception as e:
        log(f"hashrate.no unavailable: {e}")
        hrn = {}
    measured = read_json("hashrates.json", {})
    est = {}
    for key, c in CFG["coins"].items():
        rev = vol = None
        src = None
        if c.get("whattomine_id") in wtm:
            rev, vol, metrics["price"][key] = wtm[c["whattomine_id"]]
            src = "whattomine"
        elif c.get("hashrate_no") in hrn:
            rev, vol, _ = hrn[c["hashrate_no"]]
            src = "hashrate.no"
            try:
                metrics["price"][key] = hashrate_no_price(c["hashrate_no"])
            except Exception:
                pass
        if vol is None and c.get("hashrate_no") in hrn:
            vol = hrn[c["hashrate_no"]][1]
        if rev is None:
            continue
        # this GPU's measured hashrate > a benchmark set in coins.toml > hashrate.no's for GPU_MODEL
        hs = measured.get(key) or c.get("benchmark_hs") or (hrn.get(c.get("hashrate_no")) or (0, 0, 0))[2]
        if not hs:
            continue
        usd = rev * hs * (1 - c.get("dev_fee", 0)) * (1 - s.get("pool_fee", 0))
        metrics["rev"][key], metrics["hashrate"][key] = rev, hs
        metrics["source"][key] = ("measured" if key in measured else "benchmark", src)
        est[key] = {"usd_day": usd, "rev": rev, "volume": vol,
                    "liquid": vol is None or vol >= s.get("min_volume_usd", 0)}
    metrics["estimates"] = {k: v["usd_day"] for k, v in est.items()}
    return est


def check():
    s = CFG["settings"]
    st = read_json("switcher.json", {"streak": {}})
    active = read_json("active.json", None)
    if not active or active.get("coin") not in CFG["coins"]:
        key = s.get("pin") or s.get("default_coin") or next(iter(CFG["coins"]))
        active = write_profile(key, CFG["coins"][key], None, time.time())
        log(f"no active coin yet, starting with {key}")
    learn_hashrate(active)
    est = estimate()
    if not est:
        log("no market data this round, staying on " + active["coin"])
        return
    cur = active["coin"]
    if cur in est:  # keep the profile's earnings rate fresh for the exporter
        active = write_profile(cur, CFG["coins"][cur], est[cur]["rev"], active["since"])
    ranking = sorted((k for k in est if est[k]["liquid"]), key=lambda k: -est[k]["usd_day"])
    log("estimates: " + ", ".join(f"{k} ${est[k]['usd_day']:.2f}" + ("" if est[k]["liquid"] else " (thin market)")
                                  for k in sorted(est, key=lambda k: -est[k]["usd_day"])))
    pin = s.get("pin")
    if pin:
        target = pin
        reason = "pinned in coins.toml"
    else:
        best = ranking[0] if ranking else cur
        cur_usd = est.get(cur, {}).get("usd_day", 0)
        if best == cur:
            st["streak"] = {}
            target = None
        elif est[best]["usd_day"] < cur_usd * (1 + s["switch_margin"]):
            st["streak"] = {}
            target = None
        else:
            st["streak"] = {best: st["streak"].get(best, 0) + 1}
            hours = (time.time() - active["since"]) / 3600
            if st["streak"][best] < s["confirm_checks"]:
                log(f"{best} ahead of {cur} (${est[best]['usd_day']:.2f} vs ${cur_usd:.2f}), "
                    f"check {st['streak'][best]}/{s['confirm_checks']}")
                target = None
            elif hours < s["min_hours_on_coin"]:
                log(f"{best} ahead, but {cur} has only run {hours:.1f} h of {s['min_hours_on_coin']} h")
                target = None
            else:
                target = best
                reason = f"${est[best]['usd_day']:.2f}/day vs ${cur_usd:.2f}/day on {cur}"
    if target and target != cur:
        switch(target, est.get(target, {}).get("rev"), reason)
        st["streak"] = {}
    write_atomic("switcher.json", json.dumps(st))
    metrics["last_check"], metrics["ok"] = time.time(), 1


def switch(key, rev, reason):
    log(f"switching to {key} ({CFG['coins'][key]['name']}): {reason}")
    write_profile(key, CFG["coins"][key], rev, time.time())
    metrics["switches"] += 1
    time.sleep(5)  # let gpu-yield pick up the new clocks first
    if miner_running():
        docker("POST", f"/containers/{MINER}/restart?t=5")
        log("miner restarted on the new coin")
    else:
        log("miner is paused; it will start on the new coin")


# ------------------------------------------------------------------ metrics

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        active = read_json("active.json", {}) or {}
        lines = ["# TYPE switcher_est_usd_per_day gauge"]
        for k, v in metrics["estimates"].items():
            src = metrics["source"].get(k, ("", ""))
            lines.append(f'switcher_est_usd_per_day{{coin="{k}",name="{CFG["coins"][k]["name"]}",'
                         f'hashrate="{src[0]}",data="{src[1]}"}} {v:.4f}')
        lines.append("# TYPE switcher_active_coin gauge")
        for k, c in CFG["coins"].items():
            lines.append(f'switcher_active_coin{{coin="{k}",name="{c["name"]}"}} {1 if active.get("coin") == k else 0}')
        lines.append("# TYPE coin_price_usd gauge")
        for k, v in metrics["price"].items():
            if v:
                lines.append(f'coin_price_usd{{coin="{k}",name="{CFG["coins"][k]["name"]}"}} {v:.10g}')
        if metrics["btc"]:
            lines.append(f'coin_price_usd{{coin="BTC",name="Bitcoin"}} {metrics["btc"]}')
        lines.append(f"# TYPE switcher_switches_total counter\nswitcher_switches_total {metrics['switches']}")
        lines.append(f"# TYPE switcher_last_check_timestamp gauge\nswitcher_last_check_timestamp {metrics['last_check']:.0f}")
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def load_config():
    with open(CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    for opt in ("pin", "default_coin"):
        val = cfg["settings"].get(opt)
        if val and val not in cfg["coins"]:
            raise SystemExit(f"{opt} = {val!r} is not a coin in {CONFIG}")
    return cfg


if __name__ == "__main__":
    CFG = load_config()
    threading.Thread(target=lambda: ThreadingHTTPServer(("", 9836), Handler).serve_forever(), daemon=True).start()
    log(f"switcher up: {len(CFG['coins'])} coins, checking every {CFG['settings']['check_minutes']} min"
        + (f", pinned to {CFG['settings']['pin']}" if CFG["settings"].get("pin") else ""))
    while True:
        try:
            check()
        except Exception as e:
            metrics["ok"] = 0
            log(f"check failed: {e}")
        time.sleep(CFG["settings"]["check_minutes"] * 60)
