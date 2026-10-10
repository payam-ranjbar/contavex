# syntax=docker/dockerfile:1

# NOT YET VERIFIED: written while the Docker daemon was unavailable. The
# Docker verification phase must build this, then pin both images by digest
# (name:tag@sha256:...) for reproducibility.
ARG PYTHON_IMAGE=python:3.14-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.2

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
RUN groupadd --gid 10001 fleet \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin fleet \
    && install -d -o 10001 -g 10001 /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
# Readiness, checked with the service's own interpreter: no curl in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s --retries=3 \
    CMD ["python", "-m", "content_fleet", "healthcheck"]
ENTRYPOINT ["python", "-m", "content_fleet"]
CMD ["serve"]
