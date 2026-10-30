#!/bin/sh
set -eu

if [ -z "${KRYPTEX_USER:-}" ]; then
  echo "KRYPTEX_USER is not set in ~/docker/miner/.env" >&2
  exit 1
fi

# The coin switcher's choice (ALGORITHM, POOL) overrides the defaults from .env.
if [ -f /state/active.env ]; then
  . /state/active.env
  echo "Mining ${PROFILE_COIN:-?}: ${ALGORITHM} on ${POOL}"
fi

# Kryptex account mining: login is <kryptex username>/<worker name>.
exec ./SRBMiner-MULTI \
  --disable-cpu --disable-gpu-amd --disable-gpu-intel \
  --algorithm "${ALGORITHM:-pearlhash}" \
  --pool "${POOL:-prl-us.kryptex.network:7048}" \
  --wallet "${KRYPTEX_USER}/${WORKER:-homeserver}" \
  --retry-time 10 \
  --api-enable --api-port 21550 --api-rig-name "${WORKER:-homeserver}" \
  ${EXTRA_ARGS:-}
