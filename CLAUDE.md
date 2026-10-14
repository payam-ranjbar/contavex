# Contavex

A Dockerized service that publishes to Telegram, Instagram and LinkedIn
behind one HTTP API. Consumers upload media, submit a publication with an
`Idempotency-Key`, and poll it. Start with `README.md` (usage, onboarding)
and `docs/ARCHITECTURE.md` (decisions and why).

## Commands

```sh
uv sync                          # Python 3.14, exact pins in pyproject.toml / uv.lock
uv run pytest                    # unit + integration + contract; no network, ~20 s
uv run pytest -m docker          # builds the image; needs Docker Desktop running
uv run ruff check . && uv run ruff format . && uv run mypy   # all must pass
docker compose -f compose.yaml -f compose.dev.yaml -f compose.local.yaml up --build -d
```

Live tests (`tests/live/`) post to real accounts and need `-m live` and
`CONTAVEX_LIVE_TESTS=1`; never run them unasked.

## Layout

- `src/contavex/providers/<platform>/<implementation>.py`: one module per vendor API. Only `providers/registry.py` names concrete providers.
- `providers/base.py`: the `Provider` interface. `advance()` does one step and returns `Done`, `Continue` (with state) or `Unknown`; it never loops or sleeps. `worker.py` persists state between steps.
- `staging/`: makes uploaded media public for providers that fetch by URL (Instagram). Cloudflare R2 via hand-written SigV4 (`sigv4.py`).
- `credentials.py`: refreshed tokens, AES-GCM encrypted in SQLite under `secrets/master_key`.
- `tests/fakes/`: stateful HTTP-level fakes of each vendor on `httpx2.MockTransport`. `tests/contract/` runs one suite against every implementation.

## Rules

- **Secrets are files, never env vars or tracked content.** Real values live in `secrets/<name>` (git-ignored) and are referenced by name from `config/contavex.toml` (git-ignored); only `secrets/*.example` placeholders are tracked. Machine-specific compose secrets go in `compose.local.yaml` (git-ignored).
- Never log a vendor URL or request body: the Telegram token is in its URL path. Providers log an `operation` name; `observability.py` redacts as a last line of defence. Keep the canary leak tests passing.
- A call that creates a post must never be retried blindly after an ambiguous failure (timeout, 5xx after send). Reconcile with the vendor if it allows, otherwise end in `outcome_unknown`. Repeatable steps may retry.
- Vendor facts come from `docs/INTEGRATIONS/*.md` (researched from primary sources, with UNVERIFIED marks). Check there, or the vendor's current docs, before relying on remembered API behaviour. Record what a live call settles in `docs/RESEARCH.md`.
- Unsupported requests are refused with 422 before any vendor call; capabilities are data in each provider's `capabilities()`.
- Use `httpx2`, not `httpx`. No vendor SDKs. New dependencies need a reason in `docs/DEPENDENCIES.md`.
- Commit per working milestone with tests green. Do not push or publish to real accounts without being asked.
- **Never push container images** (no `docker push`, no registry login, no CI that publishes images). Images are built and run locally only.
- Per-deployment setups live in `deployments/<name>/` (compose file + config, git-ignored, never committed). `deployments/shapurx/` runs the `shapurx` container on `127.0.0.1:8090` with the Telegram @shapurx and Instagram @shapurx1 accounts; start it with `docker compose -f deployments/shapurx/compose.yaml up --build -d`. Run only one container per set of credentials: two would both renew the same Instagram token.

## Environment notes

- Windows 11 + Git Bash: write multi-line Python to a file and run it; inline heredocs with quotes or backslashes get mangled.
- Docker Desktop is often stopped; start it with `Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"`.
