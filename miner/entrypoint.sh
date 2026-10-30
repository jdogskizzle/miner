#!/bin/sh
set -eu

if [ -z "${KRYPTEX_USER:-}" ]; then
  echo "KRYPTEX_USER is not set in ~/docker/miner/.env" >&2
  exit 1
fi

# The coin switcher picks the coin (ALGORITHM, POOL). `docker compose up` can start this
# container before the switcher's first choice exists, so wait for it briefly.
waited=0
until [ -f /state/active.env ]; do
  if [ "$waited" -ge "${PROFILE_WAIT:-120}" ]; then
    echo "No coin chosen yet (no /state/active.env); gpu-yield will start mining once there is one" >&2
    exit 1
  fi
  sleep 2
  waited=$((waited + 2))
done
. /state/active.env
echo "Mining ${PROFILE_COIN:-?}: ${ALGORITHM} on ${POOL}"

# Kryptex account mining: login is <kryptex username>/<worker name>.
exec ./SRBMiner-MULTI \
  --disable-cpu --disable-gpu-amd --disable-gpu-intel \
  --algorithm "$ALGORITHM" \
  --pool "$POOL" \
  --wallet "${KRYPTEX_USER}/${WORKER:-homeserver}" \
  --retry-time 10 \
  --api-enable --api-port 21550 --api-rig-name "${WORKER:-homeserver}" \
  ${EXTRA_ARGS:-}
