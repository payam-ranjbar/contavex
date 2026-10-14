# Research

Researched: **2026-10-09**. Re-verify anything time-sensitive (API versions,
quotas, package versions) before relying on it later; LinkedIn must be
re-researched immediately before its implementation phase (REQUIREMENTS F-15).

This file is the synthesis. The detail, and the full list of every URL
opened, lives in the per-topic documents:

| Topic | Document |
|---|---|
| Telegram Bot API (10.3, released 2026-08-24) | [INTEGRATIONS/telegram.md](INTEGRATIONS/telegram.md) |
| Instagram via Meta Graph API (v26.0) | [INTEGRATIONS/instagram.md](INTEGRATIONS/instagram.md) |
| LinkedIn REST API (`LinkedIn-Version: 202609`) — initial pass | [INTEGRATIONS/linkedin.md](INTEGRATIONS/linkedin.md) |
| Docker secrets, image build, health, networking | [research/docker-and-secrets.md](research/docker-and-secrets.md) |
| Service interface, runtime stack, persistence, dependencies | [research/interface-and-stack.md](research/interface-and-stack.md) |
| Vendor test facilities, credential onboarding steps, S3 signing options | [research/testing-and-onboarding.md](research/testing-and-onboarding.md) |

## Method

- Primary sources only where they exist: `core.telegram.org`, the open-source
  Bot API server (`github.com/tdlib/telegram-bot-api`), `developers.facebook.com`,
  `learn.microsoft.com/en-us/linkedin`, `docs.docker.com`, OWASP cheat sheets,
  IETF datatracker, W3C, and package registries (PyPI/npm/Go proxy JSON APIs)
  plus the OSV advisory API.
- Nothing was written from memory. Claims that could not be confirmed in a
  source opened during the research are marked **UNVERIFIED** in the detailed
  documents and collected in the last section here.
- Limits of this research: no vendor credentials were available, so no claim
  was tested against a live vendor API. The Docker daemon was not running, so
  no Docker behaviour was reproduced locally. The Python candidate stack *was*
  installed and smoke-tested on the local interpreter (3.14.0).

## Answers to the questions that drive the design

These are the seven questions from REQUIREMENTS §8.

### 1. What is synchronous at the provider?

| Provider | Behaviour |
|---|---|
| Telegram | Synchronous. Every send/edit returns the `Message` with `message_id` in the HTTP response; albums return one id per item. Edge case: `message_id` can be `0` when Telegram auto-schedules a large video, leaving no handle for edit/delete. |
| Instagram | Asynchronous, three steps: create container → poll `status_code` (`IN_PROGRESS`/`FINISHED`/`ERROR`/`EXPIRED`/`PUBLISHED`; Meta suggests once a minute for up to five minutes) → `media_publish`. Containers expire after 24 h. Carousels add one child container per item. No webhook signals completion. |
| LinkedIn | Multi-step. Text post is one call. Media requires initialize-upload → PUT bytes → (video: multipart parts, finalize, poll processing) → create post. |

**Consequence:** a publication is a persisted, multi-step process for two of
three providers. The service contract is asynchronous for all of them
(accept, then report status); Telegram simply finishes on the first step.

### 2. How does media reach the provider?

| Provider | By URL the provider fetches | By bytes we upload |
|---|---|---|
| Telegram | Yes — photo ≤ 5 MB, other ≤ 20 MB, documents by URL limited to PDF/ZIP | Yes — photo ≤ 10 MB, other ≤ 50 MB (2000 MB via a self-hosted Bot API server) |
| Instagram (Instagram Login) | **Required.** URL must be reachable from the public internet. No image upload exists. | No |
| Instagram (Facebook Login) | Yes | Video only, via resumable upload |
| LinkedIn | **No** | **Required** for image, video, document |

**Consequence:** no single transfer mode satisfies all three. The service
must be able to *pass a URL through* (Instagram) and to *obtain bytes and
upload them* (LinkedIn, and Telegram above the URL limits). Whether a media
item is usable for a given target is a capability/validation question, not
something to hide.

### 3. Provider-side idempotency and post-hoc detection

| Provider | Idempotency key | Detecting a post after a lost response |
|---|---|---|
| Telegram | None | Impossible. Bots cannot read channel history. |
| Instagram | None. Creating a container is safely repeatable; `media_publish` is not. | Partial: container `status_code == PUBLISHED` proves the publish happened; the media id must then be recovered heuristically from the account's media list. |
| LinkedIn | None | Organization posts: yes, via the author finder (needs `r_organization_social`). Member posts: no (`r_member_social` is closed). |

**Consequence:** exactly-once publishing cannot be guaranteed by anyone.
What the service can do: (a) deduplicate caller retries with its own
idempotency keys, (b) persist every provider identifier *before* the next
non-repeatable step, (c) never automatically repeat a non-repeatable step
whose outcome is ambiguous, (d) expose an explicit **outcome unknown** state,
(e) reconcile where the provider allows it.

### 4. Static vs mutable credentials

| Provider | Credential | Lifecycle |
|---|---|---|
| Telegram | Bot token | Static, no expiry; manual rotation via BotFather. Appears in the URL path, so URLs must never be logged. |
| Instagram (Instagram Login) | Long-lived user token | 60 days; refreshable by the service (`refresh_access_token`) once it is ≥ 24 h old and not yet expired. A lapsed token needs a manual re-bootstrap in the App Dashboard. |
| LinkedIn | OAuth access token (+ refresh token for approved partners) | Access token 60 days. Refresh token 365 days, lifetime does not reset. A human must re-consent in a browser: yearly at best, roughly every 60 days on a self-serve app. |

**Consequence:** read-only injected secrets are not enough. The service
needs a small writable, encrypted credential store, a refresh job, and an
account health state that can say "re-authorization required by <date>".

### 5. Rate limits and retry hints

| Provider | Documented limits | Hint on throttle |
|---|---|---|
| Telegram | ~1 msg/s per chat, 20/min per group, ~30/s overall | 429 with `parameters.retry_after` (seconds); request was not executed, so retry is safe |
| Instagram | Publish quota per rolling 24 h — Meta's pages disagree (50 vs 100); read `content_publishing_limit` at runtime. 400 containers / 24 h. Business-use-case rate limit reported in `X-Business-Use-Case-Usage`. | Error codes 4/17/32/613/80002; estimated wait in the usage header |
| LinkedIn | Daily per app and per member, reset 00:00 UTC, numbers visible only in the developer portal | 429, no documented `Retry-After` |

**Consequence:** retry policy is per provider and per error class. Each
provider implementation classifies its own errors and supplies its own delay.

### 6. Edit and delete

| Provider | Edit | Delete |
|---|---|---|
| Telegram | Text, caption, media of the bot's own posts | Yes, within 48 h |
| Instagram | **None** (only `comment_enabled`) | Only on the Facebook Login variant |
| LinkedIn | Commentary text only | Yes |

**Consequence:** edit and delete are per-account capabilities that
consumers can discover; an unsupported one is a clean, typed refusal.

### 7. Approvals needed for operator-owned accounts

| Provider | Needed |
|---|---|
| Telegram | None. Create bot, add as channel admin with `can_post_messages`. |
| Instagram | Professional (Business/Creator) account; a Business-type Meta app; Standard Access. No App Review or Business Verification for accounts the operator owns. |
| LinkedIn | Member posting: self-serve products. Page posting: Community Management API — vetted, on a dedicated app, Development tier first. |

## Cross-cutting findings

**Direct HTTP over vendor SDKs, for all three providers.** Telegram needs
about eight methods and its SDKs are update-receiving frameworks. Meta's
SDKs are autogenerated and ads-centric, with no polling, token refresh or
resumable upload. LinkedIn's official clients are beta and last released in
2023. Owning the HTTP calls is also what lets the service own retry
classification and redaction. See [DEPENDENCIES.md](DEPENDENCIES.md).

**Instagram already has two real implementations.** "Instagram Login"
(`graph.instagram.com`, no Facebook Page, refreshable token) and "Facebook
Login" (`graph.facebook.com`, Page required, adds delete and binary video
upload) differ in host, token kind and capabilities, and one Meta app cannot
use both. This is a concrete instance of requirement F-10, not a
hypothetical one.

**Service interface.** REST/JSON with OpenAPI 3.1, `202 Accepted` plus a
status resource, an `Idempotency-Key` header, and RFC 9457 problem details.
A broker would add a stateful container without removing the need for a
database; gRPC forces a runtime on consumers of unknown language. Standards
status: RFC 9457 is a Proposed Standard; W3C Trace Context is a
Recommendation; the `Idempotency-Key` draft (‑07) **expired 2026-04-18** and
is not an RFC, so the service documents its own semantics; the `RateLimit`
header draft is still changing and is not adopted.

**Persistence and job execution.** One SQLite file (WAL) on a named volume
and an in-process worker loop over a job table give restart survival and
at-least-once step execution without a broker or second container. Accepting
a request (idempotency key + publication + first job) is one transaction.
Single replica only; PostgreSQL is the upgrade path if that changes.

**Secrets.** Docker's Compose documentation and OWASP both advise against
environment variables for secrets and for files mounted at
`/run/secrets/<name>`. Compose file secrets are not encrypted at rest. See
[SECURITY.md](SECURITY.md).

**Runtime.** Python 3.14 + FastAPI + Pydantic v2 + httpx2 + uvicorn was the
only candidate with a complete local toolchain and was verified on it. Go
would yield a smaller image and fewer dependencies but has no local
toolchain. Notable: `httpx` has had no stable release since 2024-12-06;
`httpx2` (Pydantic organisation) is the maintained continuation and is what
Starlette's test client now expects.

## Assumptions invalidated or sharpened by research

| Initial assumption | Finding |
|---|---|
| Media can be handled one way for every platform | False — see question 2. |
| A failed publication is either retryable or permanent | Incomplete — a third outcome, "unknown", is unavoidable (question 3). |
| Secrets are static values injected at start | False for Instagram and LinkedIn (question 4). |
| "Multiple implementations per platform" is a future concern | It is present from day one for Instagram. |
| `httpx` is the default Python HTTP client | Superseded by `httpx2`. |

## Testing facilities offered by the vendors

Researched for the question "is there a proper way to test these integrations?".

| Vendor | Sandbox for publishing | Machine-readable API description | What this project does |
|---|---|---|---|
| Telegram | Yes: separate test servers, reached with `/bot<token>/test/METHOD` | None official; a community JSON spec tracks Bot API 10.3 | `test_environment` account setting; the fake validates every request against the community spec |
| Instagram | None. No dry run; every publish is a real post, and this API cannot delete it | None | Stateful fake; live test against a throw-away professional account |
| LinkedIn | None. Test companies are forbidden; up to five manual test profiles are allowed | None (official Postman collections exist) | Stateful fake; live test posts with `CONNECTIONS` visibility and deletes |

Since no vendor provides a mock or a schema to validate against, the fakes
are our reading of the documentation. They make the service's own logic
testable (state machines, retries, recovery, redaction); they cannot confirm
vendor behaviour. That is what the live tests are for.

## Unverified items that matter most

The complete lists are in each detailed document. These are the ones to
settle first, by live test, when credentials exist:

1. Telegram: whether `can_post_messages` alone lets the bot *edit* its own channel posts (inferred from wording). Still open: the live run on 2026-10-10 used a bot that also had the edit and delete rights, so it does not answer this.
2. Telegram: the effective channel throttle and how album items count toward it.
3. Instagram: whether `v26.0` is served on `graph.instagram.com` (reference pages still show v25.0).
4. Instagram: the real publish quota (50 vs 100) and whether stories count.
5. Instagram: the result of a second `media_publish` on an already-published container.
6. Instagram: whether posts made while the Meta app is in Development mode are publicly visible.
7. LinkedIn: whether a self-serve app may call `/rest/posts` and `/rest/images`, or only the legacy `/v2/ugcPosts`. The documentation leans yes (the Images API page describes `w_member_social`-only tokens on the versioned gateway); the implementation uses `/rest/posts` only. A 403 on the first live post means the legacy endpoint is needed.
8. LinkedIn: refresh tokens. Per the documentation, non-partner apps do not receive them; the implementation uses one if present and otherwise reports the access token's expiry date.
9. Docker: whether a trailing newline/CRLF in a file-sourced secret is preserved (assumed yes; the service trims).
10. Docker: SQLite WAL behaviour on a Docker Desktop named volume (expected fine; bind mounts from Windows are avoided).
11. Instagram: whether Meta's media fetcher accepts presigned URLs (long query strings). If the first live publish from object storage fails to fetch, set `public_base_url` in `[staging]`.
12. Instagram: whether `Authorization: Bearer` is accepted on every `graph.instagram.com` call used (the implementation never puts the token in a URL except for the documented refresh call).

### Settled by live calls on 2026-10-11 (Instagram, read-only)

- `graph.instagram.com` serves both `v25.0` and `v26.0` (items 3).
- `Authorization: Bearer` is accepted on `/me`, `/{id}/media` and `/{id}/content_publishing_limit` (item 12).
- The publishing quota reported for a new creator account is `quota_total: 100` per 24 hours (item 4, for this account).
- `/me` returns the flat object wrapped in a `data` array, as Meta's sample shows.

Still open: everything about publishing itself (container processing, `media_publish`, the fetcher).

### Settled by live calls on 2026-10-11 (object storage)

The object-storage stager was run against a real Cloudflare R2 bucket with the service's
own SigV4 code: PUT accepted, the presigned GET URL returned the same bytes to an
anonymous client, the unsigned URL was refused (the bucket is private), DELETE removed
the object, and repeating stage and unstage was harmless. This settles the caveat about
the S3 signature test vectors having been written from memory. Whether Instagram's
fetcher accepts such a presigned URL is still open until the first publish.

### Settled by live publishing on 2026-10-11 (Instagram)

- Meta's fetcher accepts presigned R2 URLs (about 410 characters, long query string) for images and video (item 11).
- `media_publish` and container creation accept `Authorization: Bearer` with a JSON body.
- A Development-mode app with Standard Access can publish to an account added to it; no App Review was needed.
- Stories do return a permalink (`/stories/<user>/<id>`), contrary to the assumption in the integration notes.
- After five publishes (image, carousel, reel, two stories) `quota_usage` read 4. Either one kind does not count or the counter lags; not investigated.

Still unverified for Instagram: whether Development-mode posts are visible to the public (check the profile while logged out), the result of a second `media_publish` on a published container, and the real shapes of error responses.

### Settled by live calls on 2026-10-14 (LinkedIn, personal profile)

- A self-serve app (products: Share on LinkedIn; Sign In with LinkedIn using OpenID Connect) **can** create posts through the versioned `POST /rest/posts` with `LinkedIn-Version: 202609` (item 7). The answer was `201` with the post URN in `x-restli-id`, as documented. The legacy `/v2/ugcPosts` path is not needed.
- `POST /oauth/v2/introspectToken` with the app's client id and secret works for a Token Generator token and returns `expires_at`; the service uses it to show the re-authorization date.
- `GET /v2/userinfo` returned 403 for tokens carrying `openid` and `profile` for a while after the OpenID Connect product was added, then 200 with no change on the operator's side that we could see. Provisioning appears to lag; retry before debugging.
- No refresh token was issued to this self-serve app, as the documentation said (item 8).
