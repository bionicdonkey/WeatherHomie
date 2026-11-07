# Stage 1: build wheels for everything (pySTEPS has no wheels and compiles C extensions, which needs a compiler)
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels .

# Stage 2: runtime image, no compiler
FROM python:3.12-slim
ARG VERSION=dev
ARG REVISION=unknown
ARG SOURCE=
LABEL org.opencontainers.image.title="StormTracker" \
      org.opencontainers.image.description="Self-hosted storm and weather tracker for Australia: BOM radar, your weather station, storm nowcast, alerts" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="${SOURCE}"
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# libgomp: the OpenMP runtime pySTEPS' compiled extensions link against
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 app && mkdir /data && chown app /data
# the wheels are mounted for the install only, so they never become an image layer
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels pip install --no-index --find-links=/wheels stormtracker
USER app
ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["python", "-m", "stormtracker"]
