# Content Fleet

A Dockerized service that publishes social-media content to Telegram,
Instagram and LinkedIn behind one HTTP contract. Consuming applications
upload media, submit a publication and poll its status. They never see a
vendor API or a vendor credential.

## Status

Everything is built and tested against faked vendor APIs. **Telegram is also
verified against the real Bot API** (2026-10-10): text, photo, album,
document, article with slideshow, edit and delete, through the Docker stack, to a real channel.
**Instagram and LinkedIn have not been run against real accounts yet**: that
needs credentials. See [Go live](#go-live).

| Part | State |
|---|---|
| Service (API, worker, storage, secrets) | Done, tested |
| Telegram (Bot API) | Done; tested against a fake and **live against a real channel** |
| Instagram (Instagram Login) | Done, tested against a fake |
| LinkedIn (REST API) | Done, tested against a fake |
| Media staging for Instagram (S3-compatible, or self-hosted) | Done; S3 signing verified against AWS's published examples |
| Docker image and Compose | Built and smoke-tested: a consumer container publishes through the service container |

## Contents

- [Quick start](#quick-start)
- [Getting credentials for each platform](#getting-credentials-for-each-platform)
- [Using the API](#using-the-api)
- [What each platform supports](#what-each-platform-supports)
- [Running in Docker](#running-in-docker)
- [Tests](#tests)
- [Go live](#go-live)
- [Extending](#extending)
- [Documents](#documents)

## Quick start

Requirements: Docker with Compose. For development also
[uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
cp config/fleet.example.toml config/fleet.toml     # then edit: channel id, accounts
```

Create each secret named in `config/fleet.toml` as a file in `secrets/`
(git-ignored), without a trailing newline:

```powershell
# PowerShell
Set-Content -NoNewline secrets\consumer_api_key_demo   -Value '<a long random string>'
Set-Content -NoNewline secrets\telegram_news_bot_token -Value '<token from @BotFather>'
```

```sh
# bash
printf '%s' '<a long random string>'  > secrets/consumer_api_key_demo
printf '%s' '<token from @BotFather>' > secrets/telegram_news_bot_token
```

Start it, reachable from your machine on `127.0.0.1:8080`:

```sh
docker compose -f compose.yaml -f compose.dev.yaml up --build -d
curl http://127.0.0.1:8080/readyz
```

The service refuses to start, with a message naming the problem, if a secret
file is missing or an account is misconfigured.

## Getting credentials for each platform

Each platform has its own "front door". This is what to do at each one, and
what to put where afterwards. Labels are quoted from the vendors'
documentation as of 2026-10-09; dashboards change, so details may differ.
Background and sources: [docs/research/testing-and-onboarding.md](docs/research/testing-and-onboarding.md).

### Telegram

No review, no cost. You need a Telegram account and a channel you administer.

1. In Telegram, open a chat with **@BotFather** and send `/newbot`. Choose a
   display name, then a username ending in `bot`.
2. BotFather replies with a token like `110201543:AAH...`. Save it:
   `secrets/telegram_news_bot_token`.
3. Open your channel → channel info → **Administrators** → **Add Admin** →
   find the bot → grant only **Post Messages**.
4. Find the channel's numeric id (it starts with `-100`). For a public
   channel, open this in a browser, using your token and channel name:
   `https://api.telegram.org/bot<TOKEN>/getChat?chat_id=@yourchannel` and read
   `result.id`. For a private channel, post any message in it and open
   `https://api.telegram.org/bot<TOKEN>/getUpdates`; the id is
   `channel_post.chat.id`.
5. In `config/fleet.toml`:

   ```toml
   [accounts.news-channel]
   platform = "telegram"
   implementation = "bot-api"
   credentials = { token = "telegram_news_bot_token" }
   settings = { chat_id = "-1001234567890" }
   ```

If the token leaks, or to rotate it: BotFather `/token`, replace the file, restart.

**A safe place to test.** A private channel with no subscribers is enough.
Telegram also has separate test servers: log in to them from a Telegram
client (Desktop: Settings → Shift+Alt+right-click "Add Account" → "Test
Server"), create a bot with the BotFather *there*, and add
`test_environment = true` to the account's settings.

### Instagram

You need an Instagram **professional** account (Business or Creator) and a
Facebook account registered at [developers.facebook.com](https://developers.facebook.com).
No Facebook Page, no App Review and no Business Verification are needed to
publish to an account you own.

1. Go to <https://developers.facebook.com/apps/creation/> and create an app.
   When asked for a use case choose **Manage messaging and content on
   Instagram**. (Older dashboards: **Other** → app type **Business**, then
   add the **Instagram** product.)
2. In the app dashboard open the Instagram use case and select **API setup
   with Instagram Login**.
3. Under permissions make sure **`instagram_business_basic`** and
   **`instagram_business_content_publish`** are added.
4. In **Generate access tokens** click **Add account**, log in to your
   Instagram professional account, then **Generate token**. Copy the token:
   it is valid for 60 days and the service renews it by itself. Save it:
   `secrets/instagram_brand_access_token`.
5. Find the account id. Open, with your token:
   `https://graph.instagram.com/v25.0/me?fields=user_id,username&access_token=<TOKEN>`
   and take **`user_id`** (not `id`).
6. Create the encryption key for stored tokens (once per installation):

   ```sh
   python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode(),end='')" > secrets/master_key
   ```

7. In `config/fleet.toml`:

   ```toml
   [service]
   master_key = "master_key"

   [accounts.brand-instagram]
   platform = "instagram"
   implementation = "instagram-login"
   credentials = { access_token = "instagram_brand_access_token" }
   settings = { ig_user_id = "17841400000000000" }
   ```

8. Decide how uploaded files reach Instagram. Instagram downloads media
   from a public URL; it does not accept uploads. Choose one `[staging]`
   strategy in `config/fleet.toml` (both are shown, commented, in the
   example file):
   - **`object-storage`**: an S3-compatible bucket (AWS S3, Cloudflare R2,
     MinIO, Backblaze B2). The bucket stays private; Instagram gets a link
     that expires after an hour. You need an access key allowed to put and
     delete objects in that bucket.
   - **`self-hosted`**: the service serves the file itself on port 8081. You
     put that port behind an HTTPS reverse proxy that is reachable from the
     internet and give its address as `public_base_url`.

   Without `[staging]`, an Instagram account only accepts media that is
   already at a public `https://` URL.

Leave the Meta app in **Development mode**. If the token is ever unused for
60 days it lapses: repeat step 4, replace the secret file, restart.

**A safe place to test.** There is no sandbox. Every publish creates a real
post, and this API cannot delete posts. Use a second, throw-away
professional account added to the same app, and delete test posts in the
Instagram app.

### LinkedIn

LinkedIn always acts on behalf of a person who has approved your app in a
browser; there is no bot token. Posting to a **personal profile** is
self-service. Posting to a **company Page** needs a product LinkedIn
reviews.

**Personal profile**

1. Create an app at <https://www.linkedin.com/developers/apps/new>. It must
   be associated with a LinkedIn Page (any Page you administer).
2. On the app's **Products** tab add **Share on LinkedIn** and **Sign In
   with LinkedIn using OpenID Connect**.
3. On the **Auth** tab note the **Client ID** and **Client Secret**. Save
   them: `secrets/linkedin_client_id`, `secrets/linkedin_client_secret`.
4. Get an access token with LinkedIn's **Token Generator**:
   <https://www.linkedin.com/developers/tools/oauth/token-generator>. Select
   your app and the scopes `openid`, `profile`, `w_member_social`, approve,
   copy the token. Save it: `secrets/linkedin_founder_access_token`.
5. Find your author id: call
   `curl -H "Authorization: Bearer <TOKEN>" https://api.linkedin.com/v2/userinfo`
   and take `sub`. Your author is `urn:li:person:<sub>`.
6. Create `secrets/master_key` as in Instagram step 6, and in
   `config/fleet.toml`:

   ```toml
   [service]
   master_key = "master_key"

   [accounts.founder-linkedin]
   platform = "linkedin"
   implementation = "rest"
   settings = { author = "urn:li:person:AbCdEf" }
   [accounts.founder-linkedin.credentials]
   access_token = "linkedin_founder_access_token"
   client_id = "linkedin_client_id"
   client_secret = "linkedin_client_secret"
   ```

**The 60-day limit.** A LinkedIn access token lasts 60 days. LinkedIn gives
refresh tokens only to approved partners, so for a self-service app someone
must repeat step 4 about every 60 days, replace the secret file and restart.
`GET /v1/accounts` shows the date (`credential_expires_at`) and turns
`credential_status` to `expiring` ten days ahead. If LinkedIn does give your
app a refresh token, add it as `refresh_token` and the service renews the
access token itself for up to a year.

**Company Page**

1. Create a **separate** app with no other products and request
   **Community Management API** on its Products tab. LinkedIn asks for your
   legal organisation, a business email and verification by a Page admin.
2. Authorize with the scopes `w_organization_social r_organization_social
   rw_organization_admin`. The person authorizing must be an admin or
   content admin of the Page.
3. Find the Page's id:
   `curl -H "Authorization: Bearer <TOKEN>" -H "LinkedIn-Version: 202609" -H "X-Restli-Protocol-Version: 2.0.0" "https://api.linkedin.com/rest/organizationAcls?q=roleAssignee&role=ADMINISTRATOR&state=APPROVED"`.
   The author is `urn:li:organization:<id>`.
4. Configure it like the personal profile, with that author.

**A safe place to test.** There is no sandbox and LinkedIn forbids test
companies. For a personal profile, post with
`"options": {"linkedin": {"visibility": "CONNECTIONS"}}` and delete the post
afterwards (the live test does exactly that).

## Using the API

Every request carries the consumer's key. Examples use `curl`.

```sh
KEY='Authorization: Bearer <consumer key>'
BASE=http://127.0.0.1:8080

# What can I publish to, what does each account support, are its credentials healthy?
curl -H "$KEY" $BASE/v1/accounts

# Upload a file. The body is the raw file, not a form. Returns {"id": "med_..."}
curl -H "$KEY" -H 'Content-Type: image/jpeg' --data-binary @photo.jpg "$BASE/v1/media?filename=photo.jpg"

# Publish. Always answers 202 with a Location to poll.
curl -i -H "$KEY" -H 'Content-Type: application/json' -H 'Idempotency-Key: campaign-42-post-7' \
  -d '{"account":"news-channel","text":"Launch day.","media":[{"type":"image","media_id":"med_..."}]}' \
  $BASE/v1/publications

# Poll until status is no longer accepted/processing
curl -H "$KEY" $BASE/v1/publications/pub_...

# Edit or delete, where the account's capabilities allow it
curl -X PATCH  -H "$KEY" -H 'Content-Type: application/json' -d '{"text":"Corrected."}' $BASE/v1/publications/pub_...
curl -X DELETE -H "$KEY" $BASE/v1/publications/pub_...
```

A publication request:

```json
{
  "account": "brand-instagram",
  "kind": "post",
  "text": "Launch day. #news",
  "media": [{ "type": "image", "media_id": "med_...", "alt_text": "Team photo" }],
  "options": {
    "telegram": { "disable_notification": true },
    "instagram": { "share_to_feed": true },
    "linkedin": { "visibility": "PUBLIC" }
  },
  "client_reference": "campaign-42/post-7"
}
```

- `kind` is what to publish: `post` everywhere, plus `article` on Telegram
  and `reel` and `story` on Instagram. Each account lists the kinds it accepts.
- `title` is a headline, for kinds that have one (`article`).
- `media[]` items carry either a `media_id` from an upload, or a public
  `https://` `url` on accounts that accept URLs.
- `options` holds platform-specific settings, keyed by platform. Only the
  target platform's entry is used, so one body can be sent to several
  accounts.
- A request the account cannot honour is refused immediately with `422` and
  the reason. Nothing is silently dropped or downgraded.

### Telegram articles with a slideshow

`kind: "article"` sends a Telegram "rich message": one formatted message with
a headline, body text of up to 32,768 characters, and up to 50 images or
videos shown as a swipeable slideshow.

```json
{
  "account": "news-channel",
  "kind": "article",
  "title": "Launch day",
  "text": "First paragraph.\n\nSecond paragraph.",
  "media": [
    { "type": "image", "media_id": "med_1" },
    { "type": "image", "media_id": "med_2" },
    { "type": "image", "media_id": "med_3" }
  ],
  "options": { "telegram": { "media_layout": "slideshow", "media_caption": "Photos from the event" } }
}
```

- `media_layout`: `slideshow` (default), `collage`, or `stack` (each image
  on its own, with its `alt_text` as caption).
- `media_position`: `top` (default) or `bottom`. `right_to_left: true` for
  Persian or Arabic text.
- With `text_format: "plain"` the text is escaped and blank lines separate
  paragraphs. With `text_format: "html"` the body is Telegram's rich HTML:
  headings, lists, tables, block quotes, links.
- An article can be deleted through the API but not edited yet.
- Rich messages need a recent Telegram app; the public web preview
  (`t.me/s/<channel>`) shows "open Telegram to view this post" instead.

**Always send an `Idempotency-Key`**, unique per logical post, and reuse it
when retrying. The same key with the same body returns the original
publication instead of posting twice.

Publication states:

| `status` | Meaning |
|---|---|
| `accepted` | Stored and queued; nothing sent to the platform yet |
| `processing` | In progress, or waiting to retry; `error` shows the latest transient problem |
| `published` | Done; `result.posts` holds the platform's post ids and links |
| `failed` | Final. `error.retryable` says whether submitting again could work |
| `outcome_unknown` | The platform may or may not have published it and cannot tell us. The service does not retry by itself. Check the platform, then `POST /v1/publications/{id}/resolution` with `{"published": true}` or `{"published": false}` |
| `deleted` | Was published, then deleted through this API |

Errors are RFC 9457 problem documents (`application/problem+json`). The
machine-readable contract is at `GET /openapi.json`, or without a running
service: `uv run python -m content_fleet openapi`.

## What each platform supports

`GET /v1/accounts` returns this per account, with exact limits.

| | Telegram | Instagram | LinkedIn |
|---|---|---|---|
| Text only | Yes (4096 chars) | No | Yes (3000 chars) |
| Image | Yes | Yes (JPEG) | Yes |
| Video | Yes | As `reel` | Yes (MP4) |
| Document | Yes | No | Yes (PDF, Word, PowerPoint) |
| Several media in one post | Album, 2–10 | Carousel, 2–10 | 2–20 images |
| Article with slideshow | Yes (`article`, up to 50 media) | No | No |
| Story | No | Yes | No |
| Text formats | `plain`, `html` | `plain` | `plain`, `little` (LinkedIn markup) |
| Media by upload | Yes | With `[staging]` | Yes |
| Media by public URL | Yes (not documents) | Yes | No |
| Edit text afterwards | Yes | No | Yes |
| Delete afterwards | Yes (within 48 h) | No | Yes |
| After a lost response | `outcome_unknown` | Found again automatically | Page: found again. Profile: `outcome_unknown` |
| Credential upkeep | None | Renewed automatically | Manual every 60 days unless LinkedIn issues refresh tokens |

## Running in Docker

```sh
docker compose up --build -d                                    # internal network only
docker compose -f compose.yaml -f compose.dev.yaml up --build   # also on 127.0.0.1:8080
```

Other containers reach the service at `http://publisher:8080` by joining the
`content-fleet` network:

```yaml
services:
  my-app:
    networks: [content-fleet]
networks:
  content-fleet:
    external: true
```

- Every secret named in `config/fleet.toml` must also be listed under
  `secrets:` in `compose.yaml` (commented entries are provided).
- The container runs as a non-root user with a read-only filesystem. The
  `fleet-data` volume holds the publication history, uploaded media and
  encrypted tokens: back it up, and back up `secrets/master_key` separately.
- Port 8080 (the API) is never published by `compose.yaml`. Port 8081 exists
  only for the `self-hosted` staging strategy and must sit behind your HTTPS
  proxy.
- Run exactly one instance. The design relies on a single writer.

Settings are environment variables, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `CF_PORT` | `8080` | API port |
| `CF_PUBLIC_PORT` | `8081` | Public media listener (self-hosted staging only) |
| `CF_DATA_DIR` | `/data` | Database and uploaded media |
| `CF_CONFIG_FILE` | `/config/fleet.toml` | Accounts and consumers |
| `CF_SECRETS_DIR` | `/run/secrets` | Where secret files are mounted |
| `CF_LOG_LEVEL` | `INFO` | |
| `CF_MAX_UPLOAD_BYTES` | 64 MiB | Largest accepted media upload |
| `CF_MEDIA_RETENTION_SECONDS` | 48 h | How long an unused upload is kept |
| `CF_MAX_ATTEMPTS` | `5` | Consecutive failed tries of a step before giving up |
| `CF_PUBLICATION_DEADLINE_SECONDS` | 6 h | A publication not finished by then fails |
| `CF_PROVIDER_TIMEOUT_SECONDS` | `120` | Limit for one vendor step |

Without Docker, for development:

```powershell
$env:CF_DATA_DIR = ".\data"; $env:CF_CONFIG_FILE = ".\config\fleet.toml"; $env:CF_SECRETS_DIR = ".\secrets"
uv run python -m content_fleet serve
```

Logs are JSON lines on stdout with `request_id`, `publication_id`, `account`
and the vendor's own request id where it provides one. `/metrics` serves
Prometheus metrics; `/livez` and `/readyz` are the health endpoints.
How secrets are handled and kept out of logs: [docs/SECURITY.md](docs/SECURITY.md).

## Tests

```sh
uv sync
uv run pytest            # unit + integration + contract; no network, no credentials, ~30 s
uv run ruff check .
uv run mypy
```

| Suite | Command | What it covers |
|---|---|---|
| unit | `uv run pytest -m unit` | Validation, lifecycle, retries, idempotency, secrets, staging, S3 signing |
| integration | `uv run pytest -m integration` | Each provider end to end against a fake of its vendor |
| contract | `uv run pytest -m contract` | One behavioural suite that every provider implementation must pass |
| docker | `uv run pytest -m docker` | Builds the image; checks no secret is in any layer; a consumer container publishes through the service container to a fake Telegram container. Needs a Docker daemon |
| live | see below | The real vendor APIs, with real credentials |

A plain `uv run pytest` never runs `docker` or `live` tests.

**How the vendors are faked.** None of the three publishes a test server or
a machine-readable API description, and the usual HTTP mocking libraries do
not support the HTTP client used here. So each vendor has a small
hand-written fake at the HTTP layer (`tests/fakes/`) that keeps state, so
multi-step flows are real: an Instagram container has to finish processing
before it can be published; a LinkedIn post can only reference assets that
were uploaded. The Telegram fake also checks every request against a
vendored copy of a community-maintained Bot API 10.3 field list
(`tests/fixtures/telegram/`). The fakes encode our reading of the vendors'
documentation. They cannot prove the vendors behave that way; the live tests
do that.

## Go live

With credentials in hand, in this order:

1. **Telegram.** Follow [Telegram](#telegram) above, then:

   ```powershell
   $env:CF_LIVE_TESTS = "1"
   $env:CF_LIVE_TELEGRAM_TOKEN_FILE = "secrets\telegram_news_bot_token"
   $env:CF_LIVE_TELEGRAM_CHAT_ID = "-100..."
   uv run pytest -m live tests/live/test_telegram_live.py -s
   ```

   It posts, edits and deletes one message.
2. **Instagram.** `tests/live/test_instagram_live.py` publishes one image
   from a public URL to the account. The post is real and must be deleted by
   hand. The variables are listed at the top of the file.
3. **LinkedIn.** `tests/live/test_linkedin_live.py` posts to connections
   only, edits and deletes.

Live tests need both the `-m live` selection and `CF_LIVE_TESTS=1`, so they
cannot run by accident. Things only a live run can confirm are listed in
[docs/RESEARCH.md](docs/RESEARCH.md) under "Unverified items".

## Extending

**Add an account.** Add an `[accounts.<id>]` entry to `config/fleet.toml`,
create its secret file(s), list them in `compose.yaml`, restart.

**Add a consumer.** Add `[consumers.<id>]` with the name of a new API-key
secret, create the secret, restart.

**Add a platform, or a second implementation of an existing platform.**

1. Write a subclass of `Provider` (`src/content_fleet/providers/base.py`) in
   `src/content_fleet/providers/<platform>/<implementation>.py`. Declare
   `capabilities`; implement `advance` one step at a time; classify failures
   with `PublishError`; return `Unknown` when a post may have been created
   but cannot be confirmed. `telegram/bot_api.py` is the simplest reference,
   `instagram/instagram_login.py` the multi-step one.
2. List it in `default_registry()` in `providers/registry.py`, the only place
   that names concrete providers.
3. Write a fake of the vendor in `tests/fakes/` and add a `Driver` to
   `tests/contract/test_provider_contract.py`. The implementation must pass
   the whole contract suite.
4. Point an account at it with `platform` / `implementation`. Consumers
   address accounts, so nothing changes for them.

**Add a staging strategy.** Subclass `Stager` (`src/content_fleet/staging/`)
and list it in `staging/registry.py`.

## Documents

| Document | Contents |
|---|---|
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Numbered requirements, phase gates, revisions |
| [docs/RESEARCH.md](docs/RESEARCH.md) | Findings that drive the design; what is still unverified |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Decisions, alternatives considered, review after two and three providers |
| [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) | Each dependency and why; SDK versus direct API |
| [docs/SECURITY.md](docs/SECURITY.md) | Secret lifecycle, injection, redaction, rotation |
| [docs/INTEGRATIONS/](docs/INTEGRATIONS/) | Per-platform research: Telegram, Instagram, LinkedIn |
| [docs/research/](docs/research/) | Detailed research notes and full source lists |
