# Content Fleet

A Dockerized service that publishes social-media content to several
platforms behind one HTTP contract. Consuming applications upload media,
submit a publication, and poll its status; they never see a vendor API or a
vendor credential.

## Status

| Phase | State |
|---|---|
| 1. Requirements | Done — [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) |
| 2. Research | Done — [docs/RESEARCH.md](docs/RESEARCH.md) |
| 3. Architecture | Accepted — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 4. Foundation | Done, tested |
| 5. Telegram | Implemented and tested against a faked Bot API. **Not yet run against a real channel** (needs a bot token). |
| 6. Instagram | Not started |
| 7. Architecture review | Not started |
| 8. LinkedIn | Not started |
| 9–10. Hardening, Docker verification | Docker files are written but **have never been built** (no Docker daemon was available) |

## Requirements

- [uv](https://docs.astral.sh/uv/) and Python 3.14 for development
- Docker with Compose to run the service as a container

## Build and test

```sh
uv sync                 # create .venv from the lockfile
uv run pytest           # unit + integration + contract tests (no network, no credentials)
uv run ruff check .     # lint
uv run mypy             # type check
```

Test suites, selected by marker:

| Suite | Command | Needs |
|---|---|---|
| unit | `uv run pytest -m unit` | nothing |
| integration (faked vendor HTTP) | `uv run pytest -m integration` | nothing |
| contract (same behaviour for every implementation) | `uv run pytest -m contract` | nothing |
| docker | `uv run pytest -m docker` | Docker daemon — *not written yet* |
| live | see below | real credentials |

A plain `uv run pytest` never runs `docker` or `live` tests.

### Live tests

They post to a real channel, so they need two explicit opt-ins: the marker
and an environment variable.

```powershell
$env:CF_LIVE_TESTS = "1"
$env:CF_LIVE_TELEGRAM_TOKEN_FILE = "secrets\telegram_news_bot_token"
$env:CF_LIVE_TELEGRAM_CHAT_ID = "-100..."
uv run pytest -m live tests/live -s
```

## Configure

Non-secret configuration is one file; secrets are separate files it refers
to by name.

```sh
cp config/fleet.example.toml config/fleet.toml
```

Create each secret as a file in `secrets/` (git-ignored), with no trailing
newline:

```powershell
Set-Content -NoNewline secrets\consumer_api_key_demo  -Value '<a long random string>'
Set-Content -NoNewline secrets\telegram_news_bot_token -Value '<token from @BotFather>'
```

```sh
printf '%s' '<a long random string>'  > secrets/consumer_api_key_demo
printf '%s' '<token from @BotFather>' > secrets/telegram_news_bot_token
```

Process settings are environment variables; all have defaults:

| Variable | Default | Meaning |
|---|---|---|
| `CF_PORT` | `8080` | Listen port |
| `CF_DATA_DIR` | `/data` | Database and uploaded media |
| `CF_CONFIG_FILE` | `/config/fleet.toml` | Accounts and consumers |
| `CF_SECRETS_DIR` | `/run/secrets` | Where secret files are mounted |
| `CF_LOG_LEVEL` | `INFO` | |
| `CF_MAX_UPLOAD_BYTES` | 64 MiB | Largest accepted media upload |
| `CF_MEDIA_RETENTION_SECONDS` | 48 h | How long an unused upload is kept |
| `CF_MAX_ATTEMPTS` | `5` | Consecutive failed tries of a step before giving up |
| `CF_PUBLICATION_DEADLINE_SECONDS` | 6 h | A publication not finished by then fails |
| `CF_PROVIDER_TIMEOUT_SECONDS` | `120` | Limit for one vendor step, uploads included |

How secrets are handled, rotated and kept out of logs: [docs/SECURITY.md](docs/SECURITY.md).

### Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and save its token as a secret file.
2. Add the bot to your channel as an administrator with **Post messages** only.
3. Put the channel's numeric id (`-100…`) in `config/fleet.toml`.

Details and limits: [docs/INTEGRATIONS/telegram.md](docs/INTEGRATIONS/telegram.md).

## Run

With Docker (the intended way):

```sh
docker compose up --build -d                                    # internal network only
docker compose -f compose.yaml -f compose.dev.yaml up --build   # also on 127.0.0.1:8080
```

Other containers reach the service at `http://publisher:8080` by joining
the `content-fleet` network:

```yaml
services:
  my-app:
    networks: [content-fleet]
networks:
  content-fleet:
    external: true
```

Without Docker, for development:

```powershell
$env:CF_DATA_DIR = ".\data"; $env:CF_CONFIG_FILE = ".\config\fleet.toml"; $env:CF_SECRETS_DIR = ".\secrets"
uv run python -m content_fleet serve
```

## Use

Every request carries the consumer's key. Examples use `curl`.

```sh
KEY='Authorization: Bearer <consumer key>'
BASE=http://127.0.0.1:8080

# What can I publish to, and what does each account support?
curl -H "$KEY" $BASE/v1/accounts

# Upload a file (raw body, not a form). Returns {"id": "med_..."}
curl -H "$KEY" -H 'Content-Type: image/jpeg' --data-binary @photo.jpg "$BASE/v1/media?filename=photo.jpg"

# Publish. Always answers 202 with a Location to poll.
curl -i -H "$KEY" -H 'Content-Type: application/json' -H 'Idempotency-Key: 6f1c2f0e-campaign-42' \
  -d '{"account":"news-channel","text":"Launch day.","media":[{"type":"image","media_id":"med_..."}]}' \
  $BASE/v1/publications

# Poll
curl -H "$KEY" $BASE/v1/publications/pub_...

# Edit / delete, where the account's capabilities allow it
curl -X PATCH  -H "$KEY" -H 'Content-Type: application/json' -d '{"text":"Corrected."}' $BASE/v1/publications/pub_...
curl -X DELETE -H "$KEY" $BASE/v1/publications/pub_...
```

Publication states:

| `status` | Meaning |
|---|---|
| `accepted` | Stored and queued; nothing sent to the platform yet |
| `processing` | In progress or waiting to retry; `error` shows the latest transient problem |
| `published` | Done; `result.posts` holds the platform's post ids and links |
| `failed` | Final. `error.retryable` says whether submitting again could work |
| `outcome_unknown` | The platform may or may not have published it and cannot tell us. The service will not retry by itself. Check the platform, then `POST /v1/publications/{id}/resolution` |
| `deleted` | Was published, then deleted through this API |

**Always send an `Idempotency-Key`** that is unique per logical post and
reuse it when retrying. The same key with the same body returns the original
publication instead of posting twice.

Errors are RFC 9457 problem documents (`application/problem+json`). The
machine-readable contract is at `GET /openapi.json`, or without a running
service: `uv run python -m content_fleet openapi`.

## Extend

**Add an account.** Add an `[accounts.<id>]` entry to `config/fleet.toml`,
create its secret file(s), list the secret in `compose.yaml`, restart. The
service refuses to start if a referenced secret is missing or the account
settings are wrong.

**Add a consumer.** Add `[consumers.<id>]` with the name of a new API-key
secret; create the secret; restart.

**Add a platform, or a second implementation of an existing platform.**

1. Write a subclass of `Provider` (`src/content_fleet/providers/base.py`) in
   `src/content_fleet/providers/<platform>/<implementation>.py`. Declare
   `capabilities`, implement `advance` as one step at a time, classify
   failures with `PublishError`, and return `Unknown` when a post may have
   been created but cannot be confirmed. `telegram/bot_api.py` is the
   reference.
2. List it in `default_registry()` in `providers/registry.py` — the only
   place that names concrete providers.
3. Add a vendor fake and a `Driver` to `tests/contract/test_provider_contract.py`.
   The implementation must pass the whole contract suite.
4. Point an account at it with `platform` / `implementation`. Consumers
   address accounts, so nothing changes for them.

## Documents

| Document | Contents |
|---|---|
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Numbered requirements, phase gates, revisions |
| [docs/RESEARCH.md](docs/RESEARCH.md) | Findings that drive the design, unverified items |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Decisions, alternatives considered, consumer contract |
| [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) | Each dependency and why; SDK vs direct API |
| [docs/SECURITY.md](docs/SECURITY.md) | Secret lifecycle, injection, redaction, rotation |
| [docs/INTEGRATIONS/](docs/INTEGRATIONS/) | Per-platform research: Telegram, Instagram, LinkedIn |
| [docs/research/](docs/research/) | Detailed research notes and full source lists |
