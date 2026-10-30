FROM debian:bookworm-slim

ARG SRB_VERSION=3.8.0
ARG SRB_MD5=01cdd7ffdca43f3b6968fbaacea449b5

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Download the release and check it against the MD5 published in the release notes.
RUN set -eu; \
    v=$(echo "$SRB_VERSION" | tr . -); \
    curl -fsSL -o /tmp/srb.tar.gz "https://github.com/doktor83/SRBMiner-Multi/releases/download/${SRB_VERSION}/SRBMiner-Multi-${v}-Linux.tar.gz"; \
    echo "${SRB_MD5}  /tmp/srb.tar.gz" | md5sum -c -; \
    mkdir /opt/srbminer; \
    tar -xzf /tmp/srb.tar.gz -C /opt/srbminer --strip-components=1; \
    rm /tmp/srb.tar.gz

RUN useradd --system --no-create-home miner \
 && mkdir /opt/srbminer/Cache \
 && chown -R miner /opt/srbminer
USER miner
WORKDIR /opt/srbminer

COPY --chmod=755 entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
