# Architecture

Status: **ACCEPTED 2026-10-09 and implemented.** All three providers are built; the reviews in section 7 record what changed along the way.

Original decision note: **ACCEPTED 2026-10-09.** The operator decided D-2 (Python + FastAPI),
D-5 (media is uploaded by consumers; public staging is a pluggable strategy),
the Instagram variant (Instagram Login first) and D-7 (encrypt the token store).

Inputs: [REQUIREMENTS.md](REQUIREMENTS.md), [RESEARCH.md](RESEARCH.md).

## 1. Forces

What the research says the design has to cope with. Each is a fact about the
providers, not a preference.

| # | Force | Source |
|---|---|---|
| 1 | Publishing is one HTTP call for Telegram and a multi-step, minutes-long process for Instagram and LinkedIn. | RESEARCH Q1 |
| 2 | No provider offers an idempotency key. For Telegram and LinkedIn member posts, a lost response cannot be reconciled at all. | RESEARCH Q3 |
| 3 | Instagram requires media at a public URL; LinkedIn requires uploaded bytes; Telegram takes either with different limits. | RESEARCH Q2 |
| 4 | Credentials range from a static token (Telegram) to tokens the service must refresh (Instagram) to tokens that need a human in a browser (LinkedIn). | RESEARCH Q4 |
| 5 | Edit, delete, media types, limits and text formatting differ per provider and, for Instagram, per implementation. | RESEARCH Q6 |
| 6 | Rate-limit signals and retryable errors are provider-specific. | RESEARCH Q5 |
| 7 | One logical platform can have more than one implementation — already true for Instagram. | RESEARCH, cross-cutting |
| 8 | Consumers are in unknown languages and must hold no vendor knowledge or credentials. | REQUIREMENTS F-1, S-6 |
| 9 | One operator, low volume, one Docker host. | REQUIREMENTS A-1, A-3 |

## 2. Decisions

### D-1 Service interface: REST/JSON, OpenAPI 3.1, asynchronous by contract

| Option | Verdict |
|---|---|
| **REST/JSON + OpenAPI 3.1, `202` + status resource** | **Chosen.** Any language can call it with a plain HTTP client; the schema, validation and published contract come from one source; fits `Idempotency-Key`, problem details and `traceparent`. |
| gRPC / Connect | Rejected. Forces a runtime or generated code on every consumer; benefits (streaming, strict typing) are not needed. |
| Message broker (AMQP / NATS / Redis streams) | Rejected. Adds a stateful container and shared broker credentials, and the service still needs its own database to answer "what happened to my post". |
| REST + webhook callbacks | Deferred. An optional `callback_url` can be added later without breaking anything. Polling stays the source of truth. |

Every publish returns `202 Accepted` with a publication resource, including
for Telegram. A contract that is synchronous for one platform and
asynchronous for another would leak the provider into the consumer (force 1).

### D-2 Runtime: Python 3.14 + FastAPI + Pydantic + httpx2 — *accepted by operator*

| Option | For | Against |
|---|---|---|
| **Python + FastAPI** (chosen) | Only candidate with a working local toolchain, verified on this machine. OpenAPI and validation from the same models. Workload is I/O-bound and tiny. | Largest dependency set of the three (about 20 packages). FastAPI still 0.x. HTTP client ecosystem mid-move to httpx2. |
| Go + stdlib | Smallest image, single static binary, fewest third-party dependencies. | No Go toolchain installed: every test and lint cycle would run through Docker. |
| TypeScript + Fastify | Toolchain installed. | Weaker SQLite story on Node 22/24; highest advisory churn of the three. |

Detail and versions: [DEPENDENCIES.md](DEPENDENCIES.md).

### D-3 Persistence and execution: SQLite + in-process worker, one container

| Option | Verdict |
|---|---|
| **SQLite (WAL) on a named volume, worker loop over a `jobs` table** | **Chosen.** Survives restarts; accepting a request is one transaction (idempotency key + publication + first job); no second container. |
| PostgreSQL | Not yet. It is the upgrade path the day a second replica is needed. |
| Redis / external task queue | Rejected. Splits acceptance across two stores and adds infrastructure for tens of jobs a day. |
| No persistence, do the work inside the request | Rejected. Instagram's lifecycle outlives a request, and a crash would lose the identifiers needed to avoid a duplicate post (force 2). |

Consequence, accepted deliberately: **the service runs as exactly one
replica.**

### D-4 The vendor boundary: in-process provider modules behind a step interface

This is the central decision, so the alternatives are spelled out.

| Option | What it is | Verdict |
|---|---|---|
| A. `publish(request) -> result` per provider | Each provider exposes one call and does its own waiting and polling inside it. | Rejected. Simple for Telegram, wrong for Instagram: a crash mid-call loses the container id and the retry publishes twice. It hides exactly the lifecycle the brief says not to fake. |
| **B. Step interface with persisted provider state** | Each provider exposes `advance(publication, state) -> outcome`. The outcome is one of: *done*, *continue* (with new state and a delay), *failed* (classified), *unknown*. The worker persists the state between calls. | **Chosen.** Telegram returns *done* on the first step. Instagram walks create → poll → publish with a checkpoint after each. LinkedIn's upload steps fit without a new concept. |
| C. One container per provider behind an internal protocol | Each vendor integration is its own service. | Rejected for now. Real isolation of vendor credentials, but multiplies deployment and failure modes for a single operator. Option B's interface is narrow enough to move a provider out of process later. |
| D. Plugin discovery through package entry points | Providers are separately installed packages found at runtime. | Rejected. Everything lives in one repository; discovery magic buys nothing over an explicit table. |
| E. A declarative "HTTP workflow" engine configured per vendor | Describe each vendor as data. | Rejected. The vendors differ in control flow, not just in URLs. This would be a programming language in a config file. |

What a provider implementation is, concretely:

| Member | Purpose |
|---|---|
| `capabilities(account)` | What this implementation can do for this account: content kinds, media types and limits, accepted media sources (public URL / bytes), text formats, edit, delete, options schema. Served to consumers and used for validation. |
| `validate(request)` | Provider-specific checks before anything is accepted (caption length, aspect ratio hints, album rules). |
| `advance(publication, state)` | Perform the next step. Never loops or sleeps. |
| `reconcile(publication, state)` | Optional. For an *unknown* outcome, ask the provider whether the post exists. Instagram and LinkedIn organization posts can; Telegram cannot. |
| `edit(...)`, `delete(...)` | Optional; present only if declared in capabilities. Both are one provider call that converges when repeated, so they run inside the consumer's request instead of through the worker; a failure is returned as a problem document with the same error categories. |
| `step_is_repeatable(...)` | Whether the next step can run twice without risking a duplicate post. Decides what happens after a crash or a step timeout: repeat, or reconcile. |
| `check_credentials(account)` | Cheap identity call (`getMe`, `/me`) for account health and for the refresh job. |

Selection is an explicit table keyed by `(platform, implementation)`:

```
("telegram",  "bot-api")          -> TelegramBotApi
("instagram", "instagram-login")  -> InstagramLogin
("instagram", "facebook-login")   -> InstagramFacebookLogin     (later)
("linkedin",  "rest")             -> LinkedInRest               (phase E)
```

An account names its platform and implementation in configuration.
Consumers address an **account**, never an implementation, so switching
implementation is an operator edit (F-10). No named design pattern was
chosen up front; this is a lookup table and an interface, because that is
all forces 5 and 7 require.

The core depends only on this interface. Provider modules import nothing
from each other, and nothing outside `providers/` imports a provider module
except the table.

### D-5 Media: consumers upload to the service; public staging is a strategy — *decided by operator*

The operator's media originates inside the fleet and is not publicly hosted.
The first proposal (media by URL only) was therefore replaced.

**Contract.** Media is a first-class resource:

1. `POST /v1/media` — the request body is the raw file bytes, with
   `Content-Type` set. Returns a media id, size, checksum and expiry.
2. A publication references media by `media_id`.

A raw-body upload is used instead of a multipart form: it needs no inbound
multipart parser (a dependency with a poor advisory record), streams
straight to disk, and is trivial from any HTTP client. Uploaded media is
kept on the data volume and removed after a retention period.

A media item may alternatively carry a `url`. That is accepted only for
accounts whose provider fetches URLs itself (declared in capabilities as an
accepted media source). The service does **not** download consumer-supplied
URLs in the MVP, which keeps a server-side request forgery surface out of
the service entirely.

**How each provider consumes uploaded media.**

| Provider | Needs | How |
|---|---|---|
| Telegram | bytes | multipart upload from the stored file |
| LinkedIn | bytes | upload from the stored file |
| Instagram | a public HTTPS URL | **staging**: the file is made publicly reachable for the duration of the publish, then withdrawn |

**Staging is its own seam, separate from the provider.** A stager turns a
stored media item into a public URL and can withdraw it afterwards:

| Member | Purpose |
|---|---|
| `stage(media) -> public_url` | Make the file reachable from the internet. Repeatable: staging the same media twice yields a working URL. |
| `unstage(media)` | Withdraw it once the publication is terminal. |

| Stager | What it does | When to use |
|---|---|---|
| `object-storage` | Pushes the file to an S3-compatible bucket. By default the bucket stays private and the provider gets a presigned GET URL that expires (one hour unless configured, 24 hours at most); with `public_base_url` set, a URL on the operator's CDN or public domain instead | The operator has, or is willing to run, a bucket |
| `self-hosted` | The service serves the file itself at an unguessable, expiring URL on a **separate public listener**, to be placed behind the operator's reverse proxy/TLS | No external storage; the operator can expose one HTTPS path |

Stagers are selected by configuration through the same kind of explicit
table as providers. A provider that needs public URLs receives a stager
through its dependencies; it does not know which one it got. Instagram's
`advance` gains a first step, *stage*, before *create container*, and the
worker calls `unstage` when the publication reaches a terminal state.

Why a separate seam rather than part of the Instagram provider: staging is
about the operator's infrastructure, not about Meta. A second Instagram
implementation, or any future provider that fetches by URL, reuses it.

The self-hosted stager is the only part of the system reachable from the
internet. It listens on its own port, serves only currently staged files,
by random token, read-only, and exposes nothing else of the API.

Both stagers are built in phase C (Instagram). The media resource and the
stager interface are built in the foundation phase.

### D-6 Lifecycle and idempotency

Publication states:

```
            +-----------+     +------------+     +-----------+
 POST ----> | accepted  | --> | processing | --> | published |
            +-----------+     +------------+     +-----------+
                                 |   ^   |
                    retry later  +---+   +--> failed          (error.retryable = true | false)
                                         +--> outcome_unknown
```

| State | Meaning for the caller |
|---|---|
| `accepted` | Stored and queued. Nothing sent to the provider yet. |
| `processing` | Work in progress: provider-side processing, or waiting to retry. `attempts`, `next_attempt_at` and the last transient error are visible. |
| `published` | Done. The provider's post identifier(s) and URL, where one exists, are in `result`. |
| `failed` | Terminal. `error.category` says why; `error.retryable` says whether submitting again (with a new key) could succeed. |
| `outcome_unknown` | Terminal until resolved. The provider may or may not have created the post and it could not be determined. The service will **not** retry by itself. |
| `deleted` | Was published, then removed from the platform through this API. The record and its result are kept. |

`failed` with `retryable: true` means the service gave up after its own
bounded retries on a transient cause. `retryable: false` covers invalid
content, unsupported operations, authentication and policy refusals.

Error categories, one vocabulary for all providers (F-25):
`invalid_request`, `unsupported`, `authentication`, `authorization`,
`rate_limited`, `media_rejected`, `media_unreachable`, `provider_unavailable`,
`timeout`, `provider_rejected`, `malformed_provider_response`, `internal`.

Idempotency, in layers:

1. **Caller → service.** `Idempotency-Key` is required on every publish. The key, a fingerprint of the request, and the publication id are stored in the same transaction that accepts the request. Same key and same body returns the existing publication; same key and a different body is a `422`. This fully covers "the caller did not get our response and retried".
2. **Service → provider, repeatable steps.** Steps the provider treats as repeatable (creating an Instagram container, polling, initializing an upload) are retried freely.
3. **Service → provider, the non-repeatable step.** Before the step that creates the post, every identifier obtained so far is committed. If that step fails *unambiguously* (connection never established, a 4xx, a 429) it is classified and retried or failed. If it fails *ambiguously* (timeout or 5xx after the request was sent) the provider's `reconcile` is tried; if the provider cannot say, the publication becomes `outcome_unknown`.
4. **Resolving unknown.** The operator or consumer inspects the channel and marks the publication published or not published; only "not published" permits a retry.

What is and is not guaranteed is documented per provider in
`INTEGRATIONS/*.md`. In short: caller retries never duplicate; an ambiguous
provider failure never triggers an automatic duplicate; a crash of this
service in the instant between the provider accepting a post and the commit
of that fact is reported as `outcome_unknown` on restart rather than
silently retried.

### D-7 Credentials: file-injected bootstrap + encrypted store — *encryption accepted by operator*

As designed in [SECURITY.md](SECURITY.md): secrets arrive as files under
`/run/secrets/`, referenced by name from a non-secret account file; tokens
that change at runtime are kept encrypted (AES-GCM, key injected as a
secret) in the service database; one seam resolves secret names so a secret
manager can be added later.

The open point is the encryption itself. It costs one dependency
(`cryptography`). It protects volume backups and snapshots; it does not
protect against someone who has both the volume and the secrets directory.
Recommended, because backups are the likeliest way a database file leaves
the host.

### D-8 Accounts and consumer authentication

- **Accounts** are declared in `config/fleet.toml` (non-secret, mounted read-only). Adding an account is: add an entry, add its secret file, restart. No admin API in the MVP.
- **Consumers** authenticate with a bearer API key per consumer; keys are secrets like any other. The consumer's name is recorded on each publication and in logs.

### D-9 Observability

Structured JSON logs to stdout with `request_id`, `trace_id` (from an
incoming `traceparent`), `publication_id`, `account`, `platform`,
`implementation`, `step`, `attempt`, and the provider's own request id when
it supplies one (Meta `fbtrace_id`, LinkedIn `x-li-uuid`). Retry events are
logged with the delay and the category. `/livez` and `/readyz` are separate;
the Docker health check uses readiness; neither calls a vendor. `/metrics`
exposes counts and durations by platform, outcome and error category.
Redaction rules are in SECURITY.md.

## 3. Shape of the service

```
                 consumers (other containers)
                           |
                 HTTP/JSON |  Authorization: Bearer <consumer key>
                           v
 +---------------------------------------------------------------+
 |  api          validation, auth, idempotency, problem details  |
 |---------------------------------------------------------------|
 |  core         publication model, capabilities, lifecycle,     |
 |               error vocabulary, retry policy                  |
 |---------------------------------------------------------------|
 |  worker       claims due jobs, calls provider.advance(),      |
 |               persists state, schedules retries, refreshes    |
 |               credentials                                     |
 |---------------------------------------------------------------|
 |  providers    telegram/bot_api   instagram/instagram_login    |
 |               (linkedin/rest later)                           |
 |---------------------------------------------------------------|
 |  store (SQLite)   secrets (file resolver)   media store      |
 |  staging      object-storage | self-hosted   (public URLs)    |
 +---------------------------------------------------------------+
                           |
                  HTTPS    v
            Telegram / Meta / LinkedIn
```

### Consumer contract (v1)

| Method and path | Purpose |
|---|---|
| `POST /v1/publications` | Submit. Requires `Idempotency-Key`. Returns `202` + `Location`. |
| `POST /v1/media` | Upload a media file (raw body). Returns a media id. |
| `GET /v1/media/{id}` | Media metadata. |
| `GET /v1/publications/{id}` | Status, result or error. |
| `PATCH /v1/publications/{id}` | Edit published content, where the account supports it. |
| `DELETE /v1/publications/{id}` | Delete from the platform, where supported. |
| `POST /v1/publications/{id}/resolution` | Resolve an `outcome_unknown` publication. |
| `GET /v1/accounts`, `GET /v1/accounts/{id}` | Configured accounts, their capabilities and credential health. |
| `GET /openapi.json` | Machine-readable contract. |
| `GET /livez`, `GET /readyz`, `GET /metrics` | Operations. |

A publish request:

```json
{
  "account": "news-channel",
  "kind": "post",
  "text": "Launch day.",
  "text_format": "plain",
  "media": [
    { "type": "image", "media_id": "med_01JABCDEF", "alt_text": "Team photo" }
  ],
  "options": {
    "telegram":  { "disable_notification": true },
    "instagram": { "share_to_feed": true }
  },
  "client_reference": "campaign-42/post-7"
}
```

How capability differences are represented (F-3, F-4, F-5):

- **Portable core:** `text`, `media[]`, `kind`. `kind` is a small shared vocabulary (`post`, `album`, `video`, `document`, `story`, ...); each account's capabilities list which kinds it accepts.
- **Platform-specific:** `options.<platform>`, validated strictly by that provider's own schema. Namespaces for platforms other than the target are ignored, so one request body can be sent to several accounts.
- **Unsupported:** a request the target cannot honour is refused at submission with `422` and a problem document naming the capability, before any provider call. Nothing is silently dropped or downgraded.
- **Discovery:** `GET /v1/accounts/{id}` returns the capability document, including limits (caption length, media count, size limits) and accepted media sources (`upload`, `url`).

Evolution (F-6): `/v1` path prefix; additive changes only within a version;
consumers must ignore unknown fields; enumerations may grow.

### Repository layout

```
src/content_fleet/
    api/app.py          routes, auth, problem details, idempotency
    core/               models, capabilities, error vocabulary
    providers/
        base.py         the Provider interface and step outcomes
        registry.py     the (platform, implementation) table
        http.py         transport failure handling shared by all providers
        telegram/bot_api.py
        instagram/instagram_login.py
        linkedin/rest.py
    staging/            Stager interface, registry, self_hosted.py, object_storage.py, sigv4.py
    store/              sqlite access, migrations/*.sql
    worker.py           step loop, retry policy, credential refresh
    credentials.py      seed secrets + encrypted store for refreshed tokens
    media.py            uploaded-media store and retention
    secrets.py          secret resolver seam, redaction
    services.py         wiring; fails fast on bad config or missing secrets
    config.py  observability.py  __main__.py
tests/
    unit/  integration/  contract/  docker/  live/
    fakes/              stateful fakes of the three vendors
    fixtures/telegram/  Bot API field list the Telegram fake checks requests against
config/fleet.example.toml
secrets/*.example
Dockerfile  compose.yaml  compose.dev.yaml
```

## 4. Testing approach

| Suite | What runs | Provider |
|---|---|---|
| unit | Validation, capability checks, lifecycle transitions, retry policy, redaction, idempotency | none |
| integration | Each provider implementation against a scripted fake transport: success, auth failure, invalid content, rate limit, 5xx, permanent error, timeout, malformed response, media failure, retries | faked at the HTTP layer |
| contract | One behavioural suite, parametrised over every registered implementation, for each capability it declares | faked |
| docker | Build, start, health, secret injection, no secrets in layers, a second container calls the service | none |
| live | Real credentials, opt-in by explicit marker **and** environment flag; skipped otherwise | real |

The contract suite is what makes "multiple implementations of one platform"
checkable: a second Instagram implementation must pass the same tests as the
first for every capability both claim.

## 5. Deliberately left out

Admin API for accounts, downloading consumer-supplied URLs, multi-tenant
OAuth onboarding, scheduling for a
future time, webhooks to consumers, analytics, a media library, multiple
replicas. Each is additive on this design; none is needed for the Definition
of Done.

## 6. Risks carried forward

| Risk | Mitigation |
|---|---|
| Research was not tested against live vendor APIs | Live test suite; unverified items listed in RESEARCH.md are the first things to check with real credentials |
| The self-hosted stager exposes a listener to the internet | Separate port, token URLs, staged files only, withdrawn after publish; the object-storage stager avoids it entirely |
| LinkedIn requires periodic human re-consent | Account health state with expiry date; documented runbook |
| Single replica | Stated limit; PostgreSQL is the documented upgrade path |
| httpx2 is young | Import-compatible fallback to httpx 0.28.1 |

## 7. Review log

The Phase D review (after Telegram and Instagram are implemented) is
recorded here, with any changes it causes to the decisions above.

### 2026-10-09 — after the foundation and Telegram (interim note)

Built as designed, with these adjustments made while implementing:

- **`deleted` state added.** A deleted post is not "published" and not "failed".
- **A time limit on the whole provider step**, enforced by the worker. Per-operation
  HTTP timeouts restart with every chunk of an upload, so they do not bound a step.
  A step cancelled this way is treated exactly like a crash mid-step.
- **Publications double as their own jobs** (one table) rather than a separate
  `jobs` table: a publication has exactly one pending step at a time.

### 2026-10-09 — Phase D review, after Telegram and Instagram

The questions are the ones the brief asks before a third provider is added.

**Did the abstraction survive both platforms cleanly?** The step interface did. Telegram
is one step; Instagram is stage → create → (wait for children → create parent) → wait →
publish → recover, with a checkpoint between each, and the worker needed no knowledge of
any of it. Four additions to the core were needed, none of them platform-specific:

| Addition | Why | Evidence it is general |
|---|---|---|
| `PublishError(final=True)` | "This publication is over, but submitting it again may work" (an expired container). `retryable` alone could not say that: it also told the worker to repeat the step. | LinkedIn uses it for media that never finishes processing. |
| Encrypted credential store, `refresh_credentials`, credential health on accounts | Instagram's token changes while the service runs. | LinkedIn uses the same three pieces, with two mutable roles instead of one. |
| The `Stager` seam and its registry | Instagram fetches media by URL; the operator's media is not public. | Kept out of the Instagram module; a second Instagram implementation or any URL-fetching provider reuses it. |
| Checkpoints survive a retried step | A step that fails after checkpointing progress (three of four carousel children created) must resume from there. | Found by a LinkedIn test; affects Instagram too. |

**Are vendor-specific concerns leaking into consumers?** Three things a consumer sees are
vendor-shaped, and each is deliberate:

- `kind` values `reel` and `story` are Instagram's vocabulary. They are honest: there is
  no neutral word for them, and an account's capabilities list which kinds it takes.
- `options.<platform>` is vendor-specific by construction, and namespaced so it cannot
  collide or be applied to the wrong platform.
- Post ids are the vendor's own identifiers, in the vendor's format. They are opaque to
  consumers; nothing asks a consumer to parse one.

Nothing else leaks: no vendor error codes in the error category, no vendor state names in
the status, no token or URL shapes anywhere.

**Are capability differences represented honestly?** Yes, and tested per provider:
Instagram declares no text-only posts, no edit, no delete, JPEG only, and offers the
`upload` media source only when staging is configured; Telegram declares that documents
cannot be supplied by URL. Each refusal happens before any vendor call.

**Is adding another implementation straightforward?** Instagram took one module, one line
in the registry, one fake and one contract-suite driver. The contract suite ran unchanged
against it apart from one generalisation (below).

**Did we overgeneralize, or create unnecessary abstractions?**

- *Under*-generalized: the contract suite assumed every provider can publish text alone.
  Fixed by letting each driver supply its simplest supported request.
- *Duplication worth removing:* both providers classified connection failures with the
  same dozen lines, and the one distinction that matters ("could the request have reached
  the vendor?") was written twice. Extracted to `providers/http.py` before LinkedIn, which
  now uses it too. Response classification stays per provider: it is genuinely different.
- *Not extracted on purpose:* the two "recover after an unconfirmed publish" routines look
  alike but ask different questions of different APIs. One more provider would be needed
  to see a real common shape.
- *Nothing removed.* `reconcile`, `step_is_repeatable`, `Unknown` and `checkpoint` are all
  exercised by Instagram; before it they were only exercised by the scripted test provider.

**Are tests sufficiently platform-independent?** The lifecycle, idempotency, retry and
crash-recovery tests run against a scripted provider and name no platform. The contract
suite names no platform in any test body. Vendor knowledge lives in the fakes, the
drivers and the per-provider integration tests, where it belongs.

### 2026-10-09 — after LinkedIn

LinkedIn was the test of whether the design had been fitted to its first two users.

- **It fit without changing the Provider interface.** Upload → wait → create → recover is
  the same shape of state machine. Its differences landed where the design has room for
  them: the author is an account setting; byte uploads use the media store directly and
  need no stager; "little" text is an extra `text_format` in capabilities plus an escaping
  step inside the module.
- **One assumption was wrong and was fixed:** the worker assumed every credential role a
  provider calls mutable is present on every account. A LinkedIn account may have no
  refresh token.
- **A capability difference inside one platform.** A LinkedIn Page can list its posts, so
  an unconfirmed create is reconciled; a personal profile cannot, so the same situation
  ends as `outcome_unknown`. Both are the same implementation; the difference is the
  account. This is represented in behaviour and documented, but not yet as capability
  *data* a consumer could read. Worth adding (`reconciles_lost_responses`) if a consumer
  ever needs to choose on it.
- **Large uploads span steps.** A video is uploaded in 4 MB parts with the part list and
  ETags checkpointed, and a step yields after 45 seconds, so a 500 MB file neither
  exceeds the step limit nor restarts from zero after a crash.

### 2026-10-10 — Telegram articles

Telegram's "rich message" (a formatted article with a slideshow of its media) was
added as kind `article` on the existing Telegram implementation. What it needed says
something about the contract:

- **A new kind, not a new platform or endpoint.** The `kind` vocabulary plus per-account
  capabilities absorbed it. Other accounts simply do not list `article`.
- **One new portable field, `title`.** The brief listed "optional title/caption" from the
  start; no kind had needed a title until now. Kinds declare whether they take one, and a
  title on a kind without one is refused rather than dropped.
- **Per-kind text limits.** An article allows 32,768 characters where a post allows 4,096,
  so the limit moved from "per account" to "per account, overridable per kind".
- **Layout choices stayed in `options.telegram`.** Slideshow versus collage is Telegram's
  concept; it was not promoted to the common model on the strength of one platform.

### 2026-10-12 — a second implementation of one platform, for real

Requirement F-10 (one platform, several implementations) had only been exercised by
design. Deleting Instagram posts made it concrete: Meta offers deletion on its Facebook
Login API and not on the Instagram Login API, which a live call confirmed.

- `instagram / facebook-login` is a subclass of the first implementation that changes
  three things: the host, "nothing to refresh", and `delete`. It is 60 lines.
- It is one more line in the registry and one more driver in the contract suite, which
  it passes unchanged. That suite now runs one platform through two implementations.
- Nothing in the consumer contract moved. An operator switches an account by changing
  `implementation` and its credential; the account's capabilities then report
  `deletable: true`.
- The cost that could not be designed away is the operator's: a Facebook Page and a
  separate Meta app.

### Limits of what has been shown

Every provider is verified against a fake that encodes our reading of the vendor's
documentation. Telegram has also been run against the real Bot API (2026-10-10).
Instagram and LinkedIn have not: no request has reached them with a valid credential. The
live tests exist for that; until they have run, treat vendor-facing behaviour as
implemented to the documentation, not as confirmed.
