# GPU miner

Mines crypto on the server's GPU at the lowest priority: anything else that wants the
GPU (Wolf game streaming, Jellyfin, Immich, Stemmy, …) gets it within about 2 seconds,
and mining resumes once the GPU has been quiet for 2 minutes. Payouts go to a
[Kryptex](https://pool.kryptex.com) account in BTC, whichever coin is being mined.

## Services

| Service | What it does |
|---|---|
| `miner` | [SRBMiner-MULTI](https://github.com/doktor83/SRBMiner-Multi), downloaded and MD5-checked at build time. Mines the coin named in `state/active.env`. Never started by Docker itself. |
| `gpu-yield` | Watches every process on the GPU. Stops the miner when another app uses it (new process, graphics process, compute/encoder/decoder load) and starts it again after `RESUME_AFTER_IDLE` quiet seconds. Applies the active coin's power limit and clock locks only while mining, and restores full power and clocks first when yielding. |
| `switcher` | Every 30 minutes estimates what each coin in `switcher/coins.toml` would earn on this GPU (WhatToMine, falling back to hashrate.no) and switches when another coin is clearly and consistently better. Writes the choice to `state/active.env` / `state/active.json`. |
| `exporter` | Prometheus metrics: GPU power/temperature/clocks, hashrate, shares, and estimated $/day for the active coin. |

How they connect:

```
switcher ──writes──▶ state/active.env ◀──reads── miner (algorithm, pool)
   │                                  ◀──reads── gpu-yield (power limit, clocks)
   └─restarts miner on a switch       ◀──reads── exporter (state/active.json: $ rate)
gpu-yield ──starts/stops──▶ miner
```

## Setup

```sh
cp .env.example .env        # then set KRYPTEX_USER to your Kryptex username
docker compose up -d --build
```

Needs the NVIDIA Container Toolkit. `exporter` and `switcher` join the external
`grafana_default` network so Prometheus can scrape `miner-exporter:9835` and
`coin-switcher:9836`.

## Everyday use

- **Why did mining pause / resume?** `docker logs -f gpu-yield`
- **Which coin, and why?** `docker logs -f coin-switcher`
- **Turn mining off:** `MINING_ENABLED=false` in `.env`, then `docker compose up -d`
- **Force a coin:** `pin = "PRL"` in `switcher/coins.toml`, then `docker compose restart switcher`
  (a pin switches immediately; set `pin = ""` to go back to automatic)

## Switch rules (`switcher/coins.toml`)

A coin replaces the current one only when it is estimated to earn at least
`switch_margin` (15%) more on `confirm_checks` (2) checks in a row, and the current coin
has run at least `min_hours_on_coin` (6 h). Coins traded less than `min_volume_usd` per
day are skipped. Estimates use the GPU's own measured hashrate once a coin has run
20+ minutes (`state/hashrates.json`), and the `benchmark_hs` value before that.

Only coins that both the Kryptex pool and SRBMiner (on NVIDIA) support can be added.

## Tuning

Clock locks are per coin in `coins.toml`. Pearl's (`core_clock = 1500`,
`mem_clock = 810`, 250 W cap) came from a sweep on 2026-10-29 on an RTX 3080 Ti:

| Setting | Hashrate | Power |
|---|---|---|
| No locks, 250 W cap | 118.2 TH/s | 249 W |
| Memory 810 MHz | 131.3 TH/s | 249 W |
| Memory 810 + core 1500 MHz | 121.7 TH/s | 201 W |
| Memory 810 + core 1350 MHz | 109.5 TH/s | 167 W |

Pearl barely uses memory bandwidth, so slowing the memory frees power for the core.
Memory-heavy algorithms need full memory clocks, so other coins run without locks.
