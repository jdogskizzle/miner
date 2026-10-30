#!/bin/bash
# Keeps the miner at the lowest GPU priority: stops it as soon as any other
# process wants the GPU, and starts it again after the GPU has been quiet for
# RESUME_AFTER_IDLE seconds. Applies the active coin's power limit and clock locks
# (from the coin switcher's profile) only while mining.
set -u

MINER=${MINER_CONTAINER:-miner}
INTERVAL=${CHECK_INTERVAL:-2}
RESUME_AFTER_IDLE=${RESUME_AFTER_IDLE:-120}
BUSY_PCT=${BUSY_PCT:-5}            # sm/mem % that counts as "in use"
ENABLED=${MINING_ENABLED:-true}

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

# Power and clock settings come from the coin switcher's active profile (each coin has
# its own tuning in coins.toml). The miner isn't started until there is one.
PROFILE=/state/active.env
profile_sig=""
load_profile() {
  [ -f "$PROFILE" ] || return
  local sig
  sig=$(cat "$PROFILE" 2>/dev/null) || return
  [ "$sig" = "$profile_sig" ] && return
  local PROFILE_COIN="" PROFILE_POWER_LIMIT="" PROFILE_CORE_CLOCK="" PROFILE_MEM_CLOCK=""
  # shellcheck disable=SC1090
  . "$PROFILE"
  MINE_PL=${PROFILE_POWER_LIMIT:-$MINE_PL}
  CORE_CLOCK=$PROFILE_CORE_CLOCK
  MEM_CLOCK=$PROFILE_MEM_CLOCK
  [ -n "$profile_sig" ] && log "coin profile changed to ${PROFILE_COIN}: ${MINE_PL}W, core ${CORE_CLOCK:-auto}, memory ${MEM_CLOCK:-auto} MHz"
  profile_sig=$sig
  unlock_clocks   # re-locked with the new values on the next mining loop
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
    log "GPU idle for ${RESUME_AFTER_IDLE}s -> starting miner"
    api -X POST "http://d/containers/$MINER/start" >/dev/null
  fi
}

shutdown() { log "watchdog exiting"; stop_miner "watchdog stopped"; exit 0; }
trap shutdown TERM INT

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

if [ "$ENABLED" != true ] || [ -z "${KRYPTEX_USER:-}" ]; then
  log "mining disabled (MINING_ENABLED=$ENABLED, KRYPTEX_USER ${KRYPTEX_USER:+set}); keeping miner stopped"
  stop_miner "mining disabled"
  while true; do sleep 3600 & wait $!; done
fi

unlock_clocks   # start from normal clocks; they're locked again when mining starts
load_profile
log "watchdog up: default ${DEFAULT_PL}W, mining ${MINE_PL}W (core ${CORE_CLOCK:-auto}, memory ${MEM_CLOCK:-auto} MHz), resume after ${RESUME_AFTER_IDLE}s idle"
declare -A known=()
first=1
last_busy=0   # start mining right away if the GPU is idle at boot

while true; do
  load_profile
  mid=$(miner_id)
  reason=""
  declare -A seen=()
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
  unset seen
  first=0

  now=$(date +%s)
  if [ -n "$reason" ]; then
    last_busy=$now
    stop_miner "$reason"
  elif [ $((now - last_busy)) -ge "$RESUME_AFTER_IDLE" ]; then
    start_miner
  fi
  sleep "$INTERVAL" & wait $!
done
