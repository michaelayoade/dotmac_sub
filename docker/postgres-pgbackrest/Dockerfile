FROM postgis/postgis@sha256:681931a625df344215e9b8998bf34daf146b6a395ceacee4439eb9c85869239f

ARG PGBACKREST_VERSION=2.51-r0

RUN apk add --no-cache "pgbackrest=${PGBACKREST_VERSION}" \
    && pgbackrest version

LABEL org.opencontainers.image.title="DotMac PostgreSQL with pgBackRest" \
      org.opencontainers.image.description="Pinned PostgreSQL 16/PostGIS runtime with pgBackRest"
