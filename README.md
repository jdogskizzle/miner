# GPU miner

Mines crypto on the server's GPU at the lowest priority: anything else that wants the
GPU gets it within about 2 seconds,
and mining resumes once the GPU has been quiet for 2 minutes. Payouts go to a
[Kryptex](https://pool.kryptex.com) account in BTC, whichever coin is being mined.

## Services

| Service | What it does |
|---|---|
| `miner` | [SRBMiner-MULTI](https://github.com/doktor83/SRBMiner-Multi), downloaded and MD5-checked at build time. Mines the coin named in `state/active.env` (waits for the switcher's first choice). |
| `gpu-yield` | Watches every process on the GPU. Stops the miner when another app uses it (new process, graphics process, compute/encoder/decoder load) and starts it again after `RESUME_AFTER_IDLE` quiet seconds. Applies the active coin's power limit and clock locks only while mining, and restores full power and clocks first when yielding. Also runs the power-mode benchmarks (see Power modes). |
| `switcher` | Every 30 minutes estimates what each coin in `switcher/coins.toml` would earn on this GPU (WhatToMine, falling back to hashrate.no) and switches when another coin is clearly and consistently better. Writes the choice, with the power mode's clocks, to `state/active.env` / `state/active.json`. |
| `exporter` | Prometheus metrics: GPU power/temperature/clocks, hashrate, shares, the active coin and its price, and estimated $/day. |
| `control` | Web control panel (`http://<host>:CONTROL_PORT`): live status, pause/resume, power mode, pin a coin or go back to automatic, benchmark a coin, compare prices now. It has no login, so keep it on your home network. |

How they connect:

```
switcher ──writes──▶ state/active.env ◀──reads── miner (algorithm, pool)
   │                                  ◀──reads── gpu-yield (power limit, clocks)
   └─restarts miner on a switch       ◀──reads── exporter (state/active.json: $ rate)
gpu-yield ──starts/stops──▶ miner
control ──writes──▶ state/control.json ──▶ gpu-yield (pause, benchmark), switcher (pin, power mode, compare now)
gpu-yield ──benchmarks──▶ state/tuning.json ──▶ switcher (clocks for each power mode)
```

## Setup

```sh
cp .env.example .env        # set KRYPTEX_USER, GPU_MODEL and METRICS_NETWORK
docker compose up -d --build
```

Needs the NVIDIA Container Toolkit.

| `.env` setting | Meaning |
|---|---|
| `KRYPTEX_USER`, `WORKER` | Kryptex account login (`<user>/<worker>`) |
| `GPU_MODEL` | Your GPU as named on hashrate.no (`/gpus/3080ti`), for benchmark hashrates |
| `METRICS_NETWORK` | Docker network Prometheus is on; it scrapes `miner-exporter:9835` and `coin-switcher:9836` |
| `CONTROL_PORT` | Host port for the control panel (default 8095) |
| `MINING_ENABLED`, `RESUME_AFTER_IDLE`, `EXTRA_ARGS` | Kill switch, quiet time before resuming, extra miner flags |

Everything about coins (pools, region, power limits, clock tuning) is in `switcher/coins.toml`.

## Everyday use

- **Pause, power mode, pin a coin, benchmark, compare prices now:** the control panel. A pause holds until you
  resume, across restarts; other apps still take the GPU over either way. A pin switches
  within seconds; "Automatic" hands the choice back to the switch rules below.
- **Why did mining pause / resume?** `docker logs -f gpu-yield`
- **Which coin, and why?** `docker logs -f coin-switcher`
- **Turn mining off for good:** `MINING_ENABLED=false` in `.env`, then `docker compose up -d`
- **Pin a coin in config:** `pin = "PRL"` in `switcher/coins.toml`, then
  `docker compose restart switcher` (a pin set in the control panel takes precedence)

## Switch rules (`switcher/coins.toml`)

A coin replaces the current one only when it is estimated to earn at least
`switch_margin` (15%) more on `confirm_checks` (2) checks in a row, and the current coin
has run at least `min_hours_on_coin` (6 h). Coins traded less than `min_volume_usd` per
day are skipped. Estimates are for the current power mode and use, in order: the GPU's own
measured hashrate once a coin has run 20+ minutes in that mode (`state/hashrates.json`), the
coin's benchmark, its optional `benchmark_hs`, or else hashrate.no's figure for `GPU_MODEL`.

`pool_region` picks the Kryptex region for every pool (`{region}` in each address), and
`default_coin` the coin to start on (otherwise the first one listed).

Only coins that both the Kryptex pool and SRBMiner (on NVIDIA) support can be added.

## Power modes

Each coin can run in three power modes, picked in the control panel (default:
`power_mode` in `coins.toml`):

| Mode | Settings used |
|---|---|
| Max | the highest hashrate |
| Efficient | the most hashrate per watt that keeps at least 90% of max |
| Low | the most hashrate per watt that keeps at least 70% of max |

The settings come from a benchmark of that coin on this GPU. gpu-yield mines the coin at a
series of clock settings (about 10 minutes): first each supported memory clock with the core
free, then the best memory clock with the core locked at falling fractions of its maximum.
It measures hashrate and power for each and saves the three modes to `state/tuning.json`.
A benchmark mines to the pool the whole time, and stops like mining does when another app
needs the GPU.

With `auto_benchmark = true` (the default), a coin is benchmarked the first time it's
mined. Run or re-run one from the coin list in the control panel (worth it after a driver
update or a GPU change). Until a coin is benchmarked, every mode runs on its `coins.toml`
settings (`power_limit`, and optional `core_clock` / `mem_clock`).

Benchmark timing and the 90% / 70% thresholds can be changed with `BENCH_SETTLE`,
`BENCH_MEASURE`, `EFFICIENT_MIN`, `LOW_MIN` and `CORE_STEPS` in gpu-yield's environment.

Pearl's results on an RTX 3080 Ti (250 W cap), from the first sweep on 2026-10-29:

| Setting | Hashrate | Power | Mode |
|---|---|---|---|
| No locks | 118.2 TH/s | 249 W | |
| Memory 810 MHz | 131.3 TH/s | 249 W | Max |
| Memory 810 + core 1500 MHz | 121.7 TH/s | 201 W | Efficient |
| Memory 810 + core 1350 MHz | 109.5 TH/s | 167 W | Low |

Pearl barely uses memory bandwidth, so slowing the memory frees power for the core.
Memory-heavy algorithms need full memory clocks, which the benchmark finds on its own.

## License

MIT, see [LICENSE](LICENSE). This covers the code in this repository only. SRBMiner-MULTI
is a separate program, downloaded from its own releases at build time under its own terms.
