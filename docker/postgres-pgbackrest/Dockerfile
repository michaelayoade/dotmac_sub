FROM postgis/postgis@sha256:681931a625df344215e9b8998bf34daf146b6a395ceacee4439eb9c85869239f AS pgbackrest-build

ARG PGBACKREST_COMMIT=d50cfa9ee0b0b921fb6823165c65f23ff5fed0fc
ARG PGBACKREST_SOURCE_SHA256=521d6730a938d1a2cd40cbd8d75dad24f3ad4dd6943c53c1cc1453593d6c4691

RUN apk add --no-cache \
        build-base \
        bzip2-dev \
        curl \
        libxml2-dev \
        lz4-dev \
        meson \
        ninja \
        openssl-dev \
        postgresql16-dev \
        yaml-dev \
        zlib-dev \
        zstd-dev
RUN curl -fsSL \
        "https://github.com/pgbackrest/pgbackrest/archive/${PGBACKREST_COMMIT}.tar.gz" \
        -o /tmp/pgbackrest.tar.gz \
    && echo "${PGBACKREST_SOURCE_SHA256}  /tmp/pgbackrest.tar.gz" | sha256sum -c - \
    && mkdir -p /tmp/pgbackrest-source \
    && tar -xzf /tmp/pgbackrest.tar.gz --strip-components=1 -C /tmp/pgbackrest-source
RUN meson setup \
        -Dbuildtype=release \
        -Dlibssh2=disabled \
        -Dlibzstd=enabled \
        /tmp/pgbackrest-build \
        /tmp/pgbackrest-source \
    && ninja -C /tmp/pgbackrest-build

FROM postgis/postgis@sha256:681931a625df344215e9b8998bf34daf146b6a395ceacee4439eb9c85869239f

RUN apk add --no-cache lz4-libs zstd-libs
COPY --from=pgbackrest-build /tmp/pgbackrest-build/src/pgbackrest /usr/local/bin/pgbackrest
RUN pgbackrest version \
    && ! ldd /usr/local/bin/pgbackrest | grep -q 'not found'

LABEL org.opencontainers.image.title="DotMac PostgreSQL with pgBackRest" \
      org.opencontainers.image.description="Pinned PostgreSQL 16/PostGIS runtime with pgBackRest 2.58"
