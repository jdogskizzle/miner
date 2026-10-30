#!/bin/bash
# Keeps the miner at the lowest GPU priority: stops it as soon as any other
# process wants the GPU, and starts it again after the GPU has been quiet for
# RESUME_AFTER_IDLE seconds. Applies the active coin's power limit and clock locks
# (from the coin switcher's profile) only while mining.
#
# Also runs benchmarks: mines a coin at a series of clock settings, measures hashrate and
# power for each, and saves the best "max", "efficient" and "low" power modes to
# /state/tuning.json (the switcher turns the chosen mode into the coin's profile).
# Benchmarks still mine to the pool, and stop like mining does when another app needs the GPU.
#
# Can be paused, or asked for a benchmark, from the control panel (/state/control.json).
# Writes what it's doing to /state/status.json and benchmark progress to /state/benchmark.json.
set -u

MINER=${MINER_CONTAINER:-miner}
MINER_API=${MINER_API:-http://miner:21550}
INTERVAL=${CHECK_INTERVAL:-2}
RESUME_AFTER_IDLE=${RESUME_AFTER_IDLE:-120}
BUSY_PCT=${BUSY_PCT:-5}            # sm/mem % that counts as "in use"
ENABLED=${MINING_ENABLED:-true}

# Benchmark timing, and how the modes are picked from its results.
BENCH_SETTLE=${BENCH_SETTLE:-30}                # seconds after a clock change before measuring
BENCH_MEASURE=${BENCH_MEASURE:-45}              # seconds of measuring per setting
BENCH_START_TIMEOUT=${BENCH_START_TIMEOUT:-300} # for the miner's first hashrate (kernel build, pool login)
EFFICIENT_MIN=${EFFICIENT_MIN:-0.90}            # efficient: best H/W that keeps >= 90% of max hashrate
LOW_MIN=${LOW_MIN:-0.70}                        # low: best H/W that keeps >= 70%
CORE_STEPS=${CORE_STEPS:-"0.85 0.775 0.70 0.625 0.55"}   # core locks tried, as fractions of its max clock

STATE=/state
PROFILE=$STATE/active.env      # active coin, written by the switcher
COINS=$STATE/coins.json        # all coins (pools, power limits), written by the switcher
CONTROL=$STATE/control.json    # control panel choices
TUNING=$STATE/tuning.json      # benchmark results per coin
BENCH=$STATE/benchmark.json    # current / last benchmark
BENCH_ENV=$STATE/bench.env     # coin the miner mines while benchmarking (instead of active.env)
STATUS=$STATE/status.json

log() { echo "$(date '+%F %T') $*"; }
api() { curl -s --unix-socket /var/run/docker.sock "$@"; }

DEFAULT_PL=$(nvidia-smi --query-gpu=power.default_limit --format=csv,noheader,nounits | cut -d. -f1)
MINE_PL=$DEFAULT_PL   # replaced by the active coin's power limit

set_pl() {
  local cur
  cur=$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits | cut -d. -f1)
  [ "$cur" = "$1" ] || { nvidia-smi -pl "$1" >/dev/null && log "power limit ${cur}W -> $1W"; }
}

# Clock locks (tuned for the mining algorithm). Tracked in `locked` so nvidia-smi
# only runs on changes; starts as 1 so the first unlock clears leftovers.
MEM_CLOCK=""
CORE_CLOCK=""
locked=1
lock_clocks() {
  [ "$locked" = 1 ] && return
  [ -n "$MEM_CLOCK" ] && nvidia-smi -lmc "$MEM_CLOCK" >/dev/null
  [ -n "$CORE_CLOCK" ] && nvidia-smi -lgc "$CORE_CLOCK,$CORE_CLOCK" >/dev/null
  locked=1
  [ -n "$MEM_CLOCK$CORE_CLOCK" ] && log "clocks locked: core ${CORE_CLOCK:-auto}, memory ${MEM_CLOCK:-auto} MHz"
}
unlock_clocks() {
  [ "$locked" = 0 ] && return
  nvidia-smi -rgc >/dev/null 2>&1
  nvidia-smi -rmc >/dev/null 2>&1
  locked=0
  log "clocks back to normal"
}

# Power and clock settings come from the coin switcher's active profile (the coin and the
# chosen power mode). The miner isn't started until there is one.
profile_sig=""
ACTIVE_COIN=""
load_profile() {
  [ -f "$PROFILE" ] || return
  local sig
  sig=$(cat "$PROFILE" 2>/dev/null) || return
  [ "$sig" = "$profile_sig" ] && return
  local PROFILE_COIN="" PROFILE_MODE="" PROFILE_POWER_LIMIT="" PROFILE_CORE_CLOCK="" PROFILE_MEM_CLOCK=""
  # shellcheck disable=SC1090
  . "$PROFILE"
  MINE_PL=${PROFILE_POWER_LIMIT:-$MINE_PL}
  CORE_CLOCK=$PROFILE_CORE_CLOCK
  MEM_CLOCK=$PROFILE_MEM_CLOCK
  ACTIVE_COIN=$PROFILE_COIN
  [ -n "$profile_sig" ] && log "profile: ${PROFILE_COIN} (${PROFILE_MODE:-untuned}), ${MINE_PL}W, core ${CORE_CLOCK:-auto}, memory ${MEM_CLOCK:-auto} MHz"
  profile_sig=$sig
  unlock_clocks   # re-locked with the new values on the next mining loop
}

paused() { [ "$(jq -r '.paused // false' "$CONTROL" 2>/dev/null)" = true ]; }

# What the watchdog is doing, for the control panel. Rewritten only when the state changes.
status_now=""
set_status() {   # state, reason, [resume_at]
  [ "$1" = "$status_now" ] && return
  status_now=$1
  jq -n --arg state "$1" --arg reason "$2" --argjson since "$(date +%s)" \
        --argjson resume_at "${3:-null}" '{state: $state, reason: $reason, since: $since, resume_at: $resume_at}' \
    > "$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS"
}

miner_running() { [ "$(api "http://d/containers/$MINER/json" | jq -r '.State.Running // false')" = true ]; }
miner_id()      { api "http://d/containers/$MINER/json" | jq -r '.Id // empty'; }

stop_miner() {
  set_pl "$DEFAULT_PL"   # full power and clocks back first, the other app needs them now
  unlock_clocks
  if miner_running; then
    log "yield: $1 -> stopping miner"
    api -X POST "http://d/containers/$MINER/stop?t=5" >/dev/null
  fi
}

start_miner() {
  if [ -z "$profile_sig" ]; then
    [ -z "${waiting_logged:-}" ] && log "waiting for the coin switcher to pick a coin" && waiting_logged=1
    return
  fi
  set_pl "$MINE_PL"   # also covers a miner started outside the watchdog
  lock_clocks
  if ! miner_running; then
    log "GPU free -> starting miner"
    api -X POST "http://d/containers/$MINER/start" >/dev/null
  fi
}

shutdown() {
  log "watchdog exiting"
  rm -f "$BENCH_ENV"
  stop_miner "watchdog stopped"
  set_status stopped "watchdog not running"
  exit 0
}
trap shutdown TERM INT

# ------------------------------------------------------------------ watching the GPU

# Prints "pid type sm mem enc dec" for every GPU process except the miner's.
other_procs() {
  local mid="$1"
  nvidia-smi pmon -c 1 -s u 2>/dev/null | awk '!/^#/ && $2 != "-" {print $2, $3, $4, $5, $6, $7}' |
  while read -r pid type sm mem enc dec; do
    if [ -n "$mid" ] && grep -q "$mid" "/proc/$pid/cgroup" 2>/dev/null; then continue; fi
    echo "$pid $type $sm $mem $enc $dec"
  done
}

num() { [[ "$1" =~ ^[0-9]+$ ]] && echo "$1" || echo 0; }

declare -A known=()
first=1
# Sets $reason when another app wants the GPU: a new GPU process, a graphics process, or load.
scan_gpu() {
  local mid pid type sm mem enc dec name
  local -A seen=()
  mid=$(miner_id)
  reason=""
  while read -r pid type sm mem enc dec; do
    [ -z "$pid" ] && continue
    seen[$pid]=1
    name=$(cat "/proc/$pid/comm" 2>/dev/null || echo "?")
    if [ -z "${known[$pid]:-}" ]; then
      known[$pid]=1
      [ $first -eq 0 ] && reason="new GPU process $name ($pid)"
    fi
    if [[ "$type" == *G* ]]; then reason="graphics process $name ($pid)"; fi
    if [ "$(num "$sm")" -ge "$BUSY_PCT" ] || [ "$(num "$mem")" -ge "$BUSY_PCT" ] \
       || [ "$(num "$enc")" -gt 0 ] || [ "$(num "$dec")" -gt 0 ]; then
      reason="$name ($pid) busy sm=$sm enc=$enc dec=$dec"
    fi
  done < <(other_procs "$mid")
  for pid in "${!known[@]}"; do [ -n "${seen[$pid]:-}" ] || unset "known[$pid]"; done
  first=0
}

# ------------------------------------------------------------------ benchmarks

# Prints "coin requested" when the control panel has asked for a benchmark not yet run.
pending_request() {
  local req done
  req=$(jq -r '.benchmark | select(.coin) | "\(.coin) \(.requested)"' "$CONTROL" 2>/dev/null)
  [ -n "$req" ] || return 1
  done=$(jq -r '.requested // ""' "$BENCH" 2>/dev/null)
  [ "${req#* }" = "$done" ] && return 1
  echo "$req"
}

# Prints the active coin when it has never been benchmarked and auto_benchmark is on.
auto_bench_coin() {
  [ -n "$ACTIVE_COIN" ] || return 1
  [ "$(jq -r '.settings.auto_benchmark // false' "$COINS" 2>/dev/null)" = true ] || return 1
  jq -e --arg c "$ACTIVE_COIN" 'has($c)' "$TUNING" >/dev/null 2>&1 && return 1
  echo "$ACTIVE_COIN"
}

cancelled() {   # the control panel pressed cancel after this benchmark started
  jq -e --argjson t "$B_STARTED" '(.benchmark_cancel // 0) >= $t' "$CONTROL" >/dev/null 2>&1
}

update_json() {   # file, jq filter, jq args...: edits a JSON state file in place
  local file=$1 filter=$2; shift 2
  { cat "$file" 2>/dev/null || echo '{}'; } | jq "$@" "$filter" > "$file.tmp" && mv "$file.tmp" "$file"
}

bench_write() {   # status, message
  jq -n --arg coin "$B_COIN" --arg name "$B_NAME" --arg source "$B_SOURCE" --arg requested "$B_REQUESTED" \
        --arg status "$1" --arg message "$2" --argjson started "$B_STARTED" \
        --argjson step "$B_STEP" --argjson steps "$B_STEPS" --argjson current "$B_CURRENT" \
        --argjson results "$B_RESULTS" --argjson modes "$B_MODES" --argjson now "$(date +%s)" \
    '{coin: $coin, name: $name, source: $source, requested: $requested, status: $status, message: $message,
      started: $started, updated: $now, step: $step, steps: $steps, current: $current,
      results: $results, modes: $modes}' > "$BENCH.tmp" && mv "$BENCH.tmp" "$BENCH"
}

apply_clocks() {   # core, mem ("" = not locked)
  nvidia-smi -rgc >/dev/null 2>&1
  nvidia-smi -rmc >/dev/null 2>&1
  [ -n "$2" ] && nvidia-smi -lmc "$2" >/dev/null
  [ -n "$1" ] && nvidia-smi -lgc "$1,$1" >/dev/null
  return 0
}

miner_hashrate() {   # current GPU hashrate on the benchmark's algorithm (empty if none)
  curl -s -m 3 "$MINER_API" 2>/dev/null |
    jq -r --arg a "$B_ALGO" 'first(.algorithms[]? | select(.name == $a) | .hashrate.gpu.total // 0)' 2>/dev/null
}
hashing() { awk -v h="$(miner_hashrate)" 'BEGIN { exit !(h + 0 > 0) }'; }

# Sleeps n seconds while watching for reasons to stop; sets $B_ABORT and fails on one.
bench_wait() {
  local end=$((SECONDS + $1))
  while [ $SECONDS -lt "$end" ]; do
    scan_gpu
    if [ -n "$reason" ]; then B_ABORT="interrupted: $reason"; return 1; fi
    if paused; then B_ABORT="interrupted: paused from the control panel"; return 1; fi
    if cancelled; then B_ABORT="cancelled"; return 1; fi
    if ! miner_running; then B_ABORT="failed: the miner stopped"; return 1; fi
    sleep "$INTERVAL" & wait $!
  done
}

# Measures one setting; appends {core_clock, mem_clock, hashrate, power} to $B_RESULTS.
bench_step() {   # core, mem
  B_STEP=$((B_STEP + 1))
  B_CURRENT=$(jq -nc --arg c "$1" --arg m "$2" '{core_clock: ($c | tonumber? // null), mem_clock: ($m | tonumber? // null)}')
  bench_write running "measuring core ${1:-auto}, memory ${2:-auto} MHz"
  apply_clocks "$1" "$2"
  bench_wait "$BENCH_SETTLE" || return 1
  local samples="" end=$((SECONDS + BENCH_MEASURE)) hs pw h p
  while [ $SECONDS -lt "$end" ]; do
    hs=$(miner_hashrate)
    pw=$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits)
    samples+="${hs:-0} $pw"$'\n'
    bench_wait 3 || return 1
  done
  read -r h p < <(printf '%s' "$samples" |
    awk '$1 > 0 {h += $1; p += $2; n++} END {if (n) printf "%.6g %.1f\n", h / n, p / n; else print "0 0"}')
  B_RESULTS=$(jq -c --argjson cur "$B_CURRENT" --argjson h "$h" --argjson p "$p" '. + [$cur + {hashrate: $h, power: $p}]' <<<"$B_RESULTS")
  log "benchmark $B_COIN: core ${1:-auto}, memory ${2:-auto} MHz -> $h H/s at $p W"
}

run_benchmark() {   # coin, source (manual|auto), requested timestamp (manual only)
  B_COIN=$1 B_SOURCE=$2 B_REQUESTED=${3:-} B_STARTED=$(date +%s) B_STEP=0 B_STEPS=0
  B_CURRENT=null B_RESULTS='[]' B_MODES=null B_ABORT="" B_NAME=$1 B_ALGO=""
  local pl mems cores max_core waited=0 ok=1 core mem best_mem status message
  if ! jq -e --arg c "$B_COIN" '.coins | has($c)' "$COINS" >/dev/null 2>&1; then
    bench_write failed "unknown coin $B_COIN"
    return
  fi
  B_NAME=$(jq -r --arg c "$B_COIN" '.coins[$c].name' "$COINS")
  B_ALGO=$(jq -r --arg c "$B_COIN" '.coins[$c].algorithm' "$COINS")
  pl=$(jq -r --arg c "$B_COIN" --argjson d "$DEFAULT_PL" '.coins[$c].power_limit // $d' "$COINS")

  # Settings to try. Memory first (with the core unlocked): no lock, then every supported
  # memory clock between the top and the idle one. Then the core, with the best memory clock,
  # at falling fractions of its maximum, until the hashrate drops below what "low" would keep.
  mems=$(nvidia-smi --query-supported-clocks=memory --format=csv,noheader,nounits | sort -un | sed '1d;$d' | sort -rn)
  max_core=$(nvidia-smi --query-gpu=clocks.max.graphics --format=csv,noheader,nounits)
  cores=$(for f in $CORE_STEPS; do awk -v m="$max_core" -v f="$f" 'BEGIN { print int(m * f / 15) * 15 }'; done)
  B_STEPS=$(( 1 + $(grep -c . <<<"$mems") + $(grep -c . <<<"$cores") ))

  log "benchmark $B_COIN ($B_SOURCE): up to $B_STEPS settings at ${pl}W, about $(( B_STEPS * (BENCH_SETTLE + BENCH_MEASURE) / 60 + 1 )) min"
  set_status benchmarking "benchmarking $B_NAME"
  bench_write running "starting the miner on $B_NAME"
  jq -r --arg c "$B_COIN" '.coins[$c] | "PROFILE_COIN=\($c | @sh)\nALGORITHM=\(.algorithm | @sh)\nPOOL=\(.pool | @sh)"' \
    "$COINS" > "$BENCH_ENV"
  locked=1            # clocks are changed directly below; reset them afterwards
  set_pl "$pl"
  apply_clocks "" ""
  api -X POST "http://d/containers/$MINER/restart?t=5" >/dev/null

  # Wait for the first hashrate (kernel build and pool login can take a while).
  until hashing; do
    bench_wait 3 || { ok=0; break; }
    waited=$((waited + 3))
    if [ $waited -ge "$BENCH_START_TIMEOUT" ]; then B_ABORT="failed: no hashrate after ${BENCH_START_TIMEOUT}s"; ok=0; break; fi
  done

  if [ $ok = 1 ]; then
    for mem in "" $mems; do bench_step "" "$mem" || { ok=0; break; }; done
  fi
  if [ $ok = 1 ]; then
    # best memory clock; within 2% of the best, the lower clock wins (less power)
    best_mem=$(jq -r 'max_by(.hashrate).hashrate as $m | map(select(.hashrate >= 0.98 * $m))
                      | min_by(.mem_clock // 1e9) | .mem_clock // ""' <<<"$B_RESULTS")
    for core in $cores; do
      bench_step "$core" "$best_mem" || { ok=0; break; }
      if jq -e --argjson l "$LOW_MIN" 'max_by(.hashrate).hashrate * $l > .[-1].hashrate' <<<"$B_RESULTS" >/dev/null; then
        break   # slower settings couldn't be picked for any mode
      fi
    done
  fi

  rm -f "$BENCH_ENV"
  if [ $ok = 1 ] && jq -e 'any(.hashrate > 0)' <<<"$B_RESULTS" >/dev/null; then
    B_MODES=$(jq -c --argjson e "$EFFICIENT_MIN" --argjson l "$LOW_MIN" '
      map(select(.hashrate > 0 and .power > 0)) | max_by(.hashrate) as $max
      | {max: $max,
         efficient: (map(select(.hashrate >= $e * $max.hashrate)) | max_by(.hashrate / .power)),
         low: (map(select(.hashrate >= $l * $max.hashrate)) | max_by(.hashrate / .power))}' <<<"$B_RESULTS")
    update_json "$TUNING" '.[$c] = {benchmarked: $now, power_limit: $pl, modes: $modes, results: $r}' \
      --arg c "$B_COIN" --argjson now "$(date +%s)" --argjson pl "$pl" --argjson modes "$B_MODES" --argjson r "$B_RESULTS"
    status=done message="saved max, efficient and low modes"
    log "benchmark $B_COIN done: $(jq -r 'to_entries | map("\(.key) core \(.value.core_clock // "auto") mem \(.value.mem_clock // "auto")") | join(", ")' <<<"$B_MODES")"
  else
    B_ABORT=${B_ABORT:-"failed: no hashrate measured"}
    status=${B_ABORT%%:*} message=${B_ABORT#*: }
    [ "$status" = "$B_ABORT" ] && message=""
    log "benchmark $B_COIN $B_ABORT"
    # Don't retry an automatic benchmark that failed or was cancelled; one cut short by
    # another app runs again next time.
    if [ "$B_SOURCE" = auto ] && [ "$status" != interrupted ]; then
      update_json "$TUNING" '.[$c] = {error: $m, at: $now}' --arg c "$B_COIN" --arg m "$B_ABORT" --argjson now "$(date +%s)"
    fi
  fi
  B_CURRENT=null
  bench_write "$status" "$message"

  # Back to normal: the main loop re-applies the active profile (or yields).
  [ "$status" = interrupted ] && last_busy=$(date +%s)
  if [ "$B_COIN" != "$ACTIVE_COIN" ] || [ "$status" != done ]; then
    stop_miner "benchmark over"
  fi
  profile_sig=""
}

# ------------------------------------------------------------------ main loop

rm -f "$BENCH_ENV"   # left over from a benchmark cut short by a restart
if [ "$(jq -r '.status // ""' "$BENCH" 2>/dev/null)" = running ]; then
  update_json "$BENCH" '.status = "interrupted" | .message = "watchdog restarted" | .current = null'
fi

if [ "$ENABLED" != true ] || [ -z "${KRYPTEX_USER:-}" ]; then
  log "mining disabled (MINING_ENABLED=$ENABLED, KRYPTEX_USER ${KRYPTEX_USER:+set}); keeping miner stopped"
  stop_miner "mining disabled"
  set_status disabled "MINING_ENABLED=$ENABLED or no KRYPTEX_USER in .env"
  while true; do sleep 3600 & wait $!; done
fi

unlock_clocks   # start from normal clocks; they're locked again when mining starts
load_profile
log "watchdog up: default ${DEFAULT_PL}W, mining ${MINE_PL}W (core ${CORE_CLOCK:-auto}, memory ${MEM_CLOCK:-auto} MHz), resume after ${RESUME_AFTER_IDLE}s idle"
last_busy=0   # start mining right away if the GPU is idle at boot

while true; do
  load_profile
  scan_gpu

  now=$(date +%s)
  if [ -n "$reason" ]; then
    last_busy=$now
    stop_miner "$reason"
    set_status yielded "$reason"
  elif paused; then
    stop_miner "paused from the control panel"
    set_status paused "paused from the control panel"
  elif [ $((now - last_busy)) -ge "$RESUME_AFTER_IDLE" ]; then
    if req=$(pending_request); then
      run_benchmark "${req%% *}" manual "${req#* }"
      continue
    elif coin=$(auto_bench_coin); then
      run_benchmark "$coin" auto
      continue
    fi
    start_miner
    if [ -n "$profile_sig" ]; then set_status mining ""; else set_status waiting "no coin chosen yet"; fi
  else
    set_status cooling "GPU quiet, waiting ${RESUME_AFTER_IDLE}s before mining" $((last_busy + RESUME_AFTER_IDLE))
  fi
  sleep "$INTERVAL" & wait $!
done
