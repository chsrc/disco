# Minimal image for disco.
# Dependencies: bash, fio, python3, gettext (envsubst) — nothing else.
FROM ubuntu:24.04

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      fio \
      gettext \
      python3 \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/disco
COPY . /opt/disco/

# Default: scored mode against a volume mounted at /data
ENTRYPOINT ["./run-scenario.sh"]
CMD ["score", "/data/bench"]
