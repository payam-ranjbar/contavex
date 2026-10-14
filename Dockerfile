# syntax=docker/dockerfile:1

# Base images are pinned by digest so a rebuild uses the same bytes. To update:
#   docker buildx imagetools inspect python:3.14-slim   (take the top-level Digest)
# and rebuild; do the same for the uv image.
ARG PYTHON_IMAGE=python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.2@sha256:c4f5de312ee66d46810635ffc5df34a1973ba753e7241ce3a08ef979ddd7bea5

FROM ${UV_IMAGE} AS uv

# ---- build: resolve and install into a virtualenv -------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first: this layer is reused until the lockfile changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime: the virtualenv and nothing else ----------------------------
FROM ${PYTHON_IMAGE}
# The data directory exists in the image with the right owner so that a new
# named volume mounted over it inherits that ownership.
RUN groupadd --gid 10001 contavex \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin contavex \
    && install -d -o 10001 -g 10001 /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
VOLUME ["/data"]
# 8080: consumer API (internal). 8081: public media listener, only used by
# the self-hosted staging strategy.
EXPOSE 8080 8081
# Readiness, checked with the service's own interpreter: no curl in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s --retries=3 \
    CMD ["python", "-m", "contavex", "healthcheck"]
ENTRYPOINT ["python", "-m", "contavex"]
CMD ["serve"]
