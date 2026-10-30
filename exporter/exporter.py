"""Prometheus exporter for the miner: SRBMiner API stats, GPU stats from nvidia-smi, and
an earnings estimate for the active coin (rate and price from the coin switcher's profile)."""
import json
import os
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MINER_API = os.environ.get("MINER_API", "http://miner:21550")
PROFILE = os.environ.get("PROFILE_FILE", "/state/active.json")  # written by the coin switcher
PORT = int(os.environ.get("PORT", "9835"))


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

    # Active coin and earnings estimate, from the coin switcher's profile
    try:
        with open(PROFILE) as f:
            prof = json.load(f)
    except (OSError, ValueError):
        prof = {}
    if prof.get("coin"):
        lab = f'coin="{prof["coin"]}",name="{prof.get("name", prof["coin"])}"'
        out.append(f"# HELP miner_active_coin Coin being mined\n# TYPE miner_active_coin gauge\nminer_active_coin{{{lab}}} 1")
        if prof.get("price_usd"):
            out.append(f"# HELP miner_coin_price_usd Price of the coin being mined\n# TYPE miner_coin_price_usd gauge\n"
                       f"miner_coin_price_usd{{{lab}}} {prof['price_usd']:.10g}")
        if prof.get("rev_per_hs_day"):
            add("miner_est_usd_per_day", "Estimated $/day at the current hashrate, after fees",
                round(hashrate * prof["rev_per_hs_day"] * prof.get("fee_factor", 1), 4))
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
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
