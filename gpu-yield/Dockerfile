FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl jq \
 && rm -rf /var/lib/apt/lists/*

COPY --chmod=755 gpu-yield.sh /gpu-yield.sh
ENTRYPOINT ["/gpu-yield.sh"]
