"""Prometheus exporter for the miner: SRBMiner API stats, GPU stats from nvidia-smi,
and an earnings estimate based on Pearl's current revenue per TH/s (hashrate.no)."""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MINER_API = os.environ.get("MINER_API", "http://miner:21550")
REVENUE_URL = os.environ.get("REVENUE_URL", "https://www.hashrate.no/coins/PRL")
FEE_FACTOR = float(os.environ.get("FEE_FACTOR", "0.97"))  # 2% miner dev fee + 1% pool fee
PORT = int(os.environ.get("PORT", "9835"))

revenue = {"usd_per_ths_day": None, "prl_usd": None, "updated": 0}


def refresh_revenue():
    while True:
        try:
            req = urllib.request.Request(REVENUE_URL, headers={"User-Agent": "Mozilla/5.0"})
            text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
            rev = re.search(r"Est\. Revenue Up to date \$([0-9.]+)", text)
            price = re.search(r"Price Up to date \$([0-9.]+)", text)
            if rev:
                revenue["usd_per_ths_day"] = float(rev.group(1))
                revenue["updated"] = time.time()
            if price:
                revenue["prl_usd"] = float(price.group(1))
        except Exception as e:
            print("revenue fetch failed:", e, flush=True)
        time.sleep(1800)


GPU_FIELDS = {
    "power.draw": ("gpu_power_watts", "GPU power draw"),
    "power.limit": ("gpu_power_limit_watts", "GPU power limit"),
    "temperature.gpu": ("gpu_temperature_celsius", "GPU core temperature"),
    "fan.speed": ("gpu_fan_percent", "GPU fan speed"),
    "utilization.gpu": ("gpu_utilization_percent", "GPU utilization"),
    "utilization.encoder": ("gpu_encoder_utilization_percent", "NVENC utilization"),
    "memory.used": ("gpu_memory_used_mib", "GPU memory used"),
    "clocks.sm": ("gpu_core_clock_mhz", "GPU core clock"),
}


def metrics():
    out = []

    def add(name, help_, value, kind="gauge"):
        if value is None:
            return
        out.append(f"# HELP {name} {help_}\n# TYPE {name} {kind}\n{name} {value}")

    # GPU
    try:
        q = ",".join(GPU_FIELDS)
        line = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        for (field, (name, help_)), val in zip(GPU_FIELDS.items(), line.split(",")):
            try:
                add(name, help_, float(val))
            except ValueError:
                pass
    except Exception as e:
        print("nvidia-smi failed:", e, flush=True)

    # Miner
    hashrate = 0.0
    try:
        d = json.load(urllib.request.urlopen(MINER_API, timeout=3))
        a = d["algorithms"][0]
        hashrate = float(a["hashrate"]["1min"] or 0)
        add("miner_up", "1 if the miner is running", 1)
        add("miner_hashrate_hs", "Miner hashrate (1 min average), H/s", hashrate)
        add("miner_hashrate_1h_hs", "Miner hashrate (1 hour average), H/s", float(a["hashrate"]["1hr"] or 0))
        add("miner_shares_accepted", "Accepted shares since miner start", a["shares"]["accepted"])
        add("miner_shares_rejected", "Rejected shares since miner start", a["shares"]["rejected"])
        add("miner_pool_latency_ms", "Pool latency", a["pool"]["latency"])
        add("miner_uptime_seconds", "Seconds since miner start", d.get("mining_time"))
    except Exception:
        add("miner_up", "1 if the miner is running", 0)  # stopped or paused by gpu-yield

    # Earnings estimate
    rate = revenue["usd_per_ths_day"]
    add("pearl_revenue_usd_per_ths_day", "Pearl revenue per TH/s per day (hashrate.no)", rate)
    add("pearl_price_usd", "Pearl price (hashrate.no)", revenue["prl_usd"])
    # Value the hashrate with the active coin's rate from the coin switcher; fall back to
    # Pearl's hashrate.no rate if the switcher hasn't written one yet.
    est = None
    try:
        with open("/state/active.json") as f:
            prof = json.load(f)
        if prof.get("rev_per_hs_day"):
            est = hashrate * prof["rev_per_hs_day"] * prof.get("fee_factor", FEE_FACTOR)
    except (OSError, ValueError):
        pass
    if est is None and rate is not None:
        est = hashrate / 1e12 * rate * FEE_FACTOR
    if est is not None:
        add("miner_est_usd_per_day", "Estimated $/day at the current hashrate, after fees", round(est, 4))
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = metrics().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=refresh_revenue, daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
