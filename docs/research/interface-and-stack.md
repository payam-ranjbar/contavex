# Research notes — service interface, runtime stack, persistence, dependencies

Research date: 2026-10-09. Raw material for RESEARCH.md / DEPENDENCIES.md / architecture doc.

Method: versions, dates, licences, `requires_python`/`engines`, wheel tags and dependency lists were read
from the registries' JSON APIs on the research date (PyPI JSON API, npm registry, Go module proxy, Docker Hub
API, nodejs.org index, endoflife.date API). Advisories were read from the OSV API (`api.osv.dev/v1/query`),
filtered to `published >= 2024-10-01`. Standards status was read from the IETF datatracker API and document
pages. The Python candidate stack was additionally installed and smoke-tested locally on Python 3.14.0
(Windows, uv). Anything not confirmed this way is marked **UNVERIFIED**.

---

## Part 1 — Service interface

### 1.1 Options compared (for this service)

| Criterion | REST/JSON + OpenAPI 3.1 | gRPC / protobuf | Connect (protobuf, HTTP POST) | Broker (AMQP / NATS / Redis Streams) | Hybrid: REST + webhook callback | Hybrid: REST + SSE |
|---|---|---|---|---|---|---|
| Interop with unknown-language callers | Best: any HTTP client, curl | Needs gRPC runtime + HTTP/2 per caller language | Unary calls are plain HTTP/1.1 POST with JSON; official impls: Go, Web, Node, Python, Kotlin, Swift, Dart | Every caller needs a broker client + shared broker credentials | Same as REST; caller must also host an HTTP endpoint | Same as REST; caller needs an SSE reader |
| Simplicity (ops) | One port, one container | One port, HTTP/2 end-to-end, proto toolchain | One port, proto toolchain | Extra stateful container; service still needs its own DB for status | REST + outbound delivery, signing, retry of callbacks | REST + long-lived connections |
| Schema definition | OpenAPI 3.1 (JSON Schema 2020-12) | `.proto` | `.proto` | None standard (AsyncAPI optional) | OpenAPI (`webhooks` object exists in 3.1) | OpenAPI describes it poorly |
| Validation | From schema (Pydantic / ajv / etc.) | Type-level only; semantic rules need protovalidate or code | Same as gRPC | Hand-rolled | As REST | As REST |
| Generated clients | openapi-generator etc., many languages; optional because raw HTTP is trivial | Mandatory, good | Mandatory for typed use; JSON fallback works | None | As REST | As REST |
| Async operations | `202 Accepted` + `Location` + status resource, polling | Operation message + Get (AIP-151) or server streaming | Same as gRPC | Natural for submit; result needs reply queue or status API anyway | Push on terminal state; polling still needed as fallback | Push while connected; polling still needed as fallback |
| Media transfer | URL reference in JSON; multipart possible | Bytes in message (default 4 MB message cap is a common gRPC default — UNVERIFIED) or client streaming | Same | Poor fit for large payloads | As REST | As REST |
| Observability | Access logs, status codes, `traceparent` header, any proxy | Needs gRPC-aware tooling | HTTP-level tooling works for unary | Needs broker tooling; trace context carried in message headers by convention | As REST | As REST |
| Retries | `Idempotency-Key` header + safe retry on 5xx/timeouts | Same idea, hand-defined field/metadata | Same | Redelivery built in; dedupe still needed | Callback delivery needs its own retry policy | Reconnect with `Last-Event-ID` |
| Versioning / back-compat | `/v1` path prefix; additive fields; ignore-unknown | Field numbers, strong rules | Same as gRPC | Message-version field by convention | As REST | As REST |

### 1.2 What current standards say (checked 2026-10-09)

| Topic | Status | Facts |
|---|---|---|
| `Idempotency-Key` header | **Internet-Draft, expired.** `draft-ietf-httpapi-idempotency-key-header-07`, submitted 2025-10-15, expired 2026-04-18, httpapi WG document, no IESG progress. Not an RFC. | Value is a Structured Field Item that MUST be a String, e.g. `Idempotency-Key: "8e03978e-40d5-43e8-bc93-6894a57f9324"`. Key MUST be unique and MUST NOT be reused with a different payload; UUID recommended. Server SHOULD publish an expiry policy. Optional "idempotency fingerprint" (hash of payload) to detect misuse. Errors: missing required key → 400; key reused with different payload → 422; original request still in progress → 409. Retry of a completed request → return the stored result. Targets POST and PATCH. |
| Problem Details | **RFC 9457**, Standards Track (Proposed Standard), July 2023, obsoletes RFC 7807. | Media type `application/problem+json`. Members `type` (default `about:blank`), `title`, `status`, `detail`, `instance`; extension members allowed, clients MUST ignore unknown ones. Report the most relevant problem rather than batching unrelated ones. |
| RateLimit header fields | **Internet-Draft, active.** `draft-ietf-httpapi-ratelimit-headers-11`, submitted 2026-05-23, expires 2026-11-24, intended Standards Track. Not an RFC. | Two Structured-Field response headers: `RateLimit-Policy: "burst";q=100;w=60` and `RateLimit: "default";r=50;t=30`. Replaces the older three-header `RateLimit-Limit/-Remaining/-Reset` design of earlier drafts and the `X-RateLimit-*` family. Still changing shape between drafts. |
| Trace Context | **W3C Recommendation** (Level 1), 2021-11-23. Level 2 status not confirmed from the page fetched — UNVERIFIED. | `traceparent: 00-<32 hex trace-id>-<16 hex parent-id>-<2 hex flags>`; optional `tracestate`. |
| Long-running operations | Google **AIP-151**, state Approved. | Return an `Operation` (`name`, `done`, `metadata`, and `response` or `error`); client polls a Get on the operation; guideline threshold ~10 s. HTTP equivalent: `202 Accepted` with `Location` pointing at a status resource (RFC 9110 semantics — not re-fetched, UNVERIFIED wording). |
| OpenAPI | Latest published OAS is **3.2.1** (2026-09-10); latest 3.1 patch is 3.1.2. | 3.1+ uses JSON Schema 2020-12. FastAPI and Litestar emit `3.1.0` (FastAPI confirmed locally: `app.openapi()["openapi"] == "3.1.0"`). 3.2 tool support not investigated — UNVERIFIED. |

### 1.3 Media transfer

| Mode | Fit |
|---|---|
| Media by URL reference (`media: [{url, type}]`) | Matches Instagram, which requires media at a publicly reachable URL (owned by the Instagram research slice). Telegram Bot API also accepts HTTP URLs for media (owned by the Telegram slice; limits differ from upload). Keeps the API pure JSON, keeps request bodies small, makes idempotency fingerprinting trivial, and avoids a multipart parser in the service. |
| Multipart upload to the service | Works for Telegram (service re-uploads). Does not work for Instagram unless the service also hosts the file at a public URL, which turns the service into a public file server. Adds a multipart parser dependency (python-multipart: 8 distinct advisories since 2024-10). |
| Presigned upload (object storage) | Solves Instagram properly but adds an object store and a public endpoint. Not justified at tens to hundreds of posts/day. Can be added later without changing the publish contract, because the result is still a URL. |

### 1.4 Recommendation

**REST/JSON over HTTP, described by OpenAPI 3.1, with 202 + status resource; media by URL reference; no broker, no gRPC.**

Decisive reasons: callers are in unknown languages (plain HTTP+JSON needs no generated code or runtime); the
service needs a durable publication record anyway, so a broker would add a stateful container without removing
the database; single operator, low volume.

Concrete contract conventions:

- `POST /v1/publications` → `202 Accepted`, `Location: /v1/publications/{id}`, body = publication resource
  (`id`, `status`, `target`, `created_at`, `updated_at`, `attempts`, `result` | `error`).
  `status` ∈ `accepted | processing | published | failed_retryable | failed_permanent`.
- `GET /v1/publications/{id}` for polling; include `Retry-After` on non-terminal responses as a polling hint.
- `Idempotency-Key` request header required on POST. Follow draft-07 semantics (400 missing, 422 same key
  with different payload, 409 while the first request is still being accepted, replay stored response
  otherwise). Store key + payload fingerprint + publication id; publish the retention period. Because the
  draft is expired, document the behaviour in the service's own OpenAPI rather than by reference only.
  Accept both quoted (Structured Field) and bare values — most existing clients send bare strings (inference).
- Errors as RFC 9457 `application/problem+json` with a stable `type` URI per error class and extension
  members (`publication_id`, `upstream`, `retryable`). FastAPI's default validation error is
  `422 application/json` (confirmed locally) — a custom exception handler is needed.
- Accept `traceparent`; log trace-id on every record; forward nothing to the social platforms.
- Rate limiting: return `429` + `Retry-After` when the service itself sheds load. Do not emit the draft
  `RateLimit` headers yet (draft still changing, single operator). Upstream rate limits are handled by the
  worker, surfaced in the publication's `error`/`next_attempt_at`.
- Versioning: `/v1` prefix, additive changes only, callers must ignore unknown fields, enum values may grow.
- Optional later, additive: `callback_url` per request (signed POST on terminal state). Polling stays the
  source of truth. SSE is not worth it for this workload.

---

## Part 2 — Runtime stack

### 2.1 Runtime status (2026-10-09)

| Runtime | Latest | Notes |
|---|---|---|
| Python 3.14 | 3.14.8 (2026-09-30) | Bug-fix support until 2027-10-01, EOL 2030-10-31. Local machine has 3.14.0. |
| Python 3.13 | 3.13.16 (2026-09-30) | Bug-fix phase ended 2026-10-01 (security-only now), EOL 2029-10-31. |
| Node 22 "Jod" | 22.23.3 (2026-09-23) | LTS line in maintenance; EOL date not confirmed this session (expected 2027-04) — UNVERIFIED. |
| Node 24 "Krypton" | 24.21.0 (2026-09-07) | Active LTS until 2026-10-20, EOL 2028-04-30. |
| Node 26 | 26.11.1 (2026-10-07) | Current; becomes LTS 2026-10-28; EOL 2029-04-30. |
| Go | 1.27.2 and 1.26.9 (both 2026-10-08); 1.27.0 released 2026-08-19 | Each major supported until two newer majors exist. No local toolchain. |

### 2.2 Candidates

| | (a) Python + FastAPI | (b) TypeScript + Fastify / Hono | (c) Go + net/http |
|---|---|---|---|
| Web framework | FastAPI 0.143.0 (2026-10-08, MIT, py>=3.10) on Starlette 1.7.0 (2026-09-23, BSD-3). Alt: Litestar 2.24.0 (2026-06-11, MIT; 3.0 in development) | Fastify 5.12.5 (2026-09-16, MIT; 6.0.0-alpha on `next`). Alt: Hono 4.13.13 (2026-10-04, MIT, zero deps; 5.0.0-rc.0 on `next`) + @hono/node-server 2.1.4 | stdlib `net/http` ServeMux (method + `{wildcard}` patterns, `PathValue`, Go >= 1.22) |
| Validation | Pydantic 2.14.0 / pydantic-core 2.50.0 (2026-10-08, MIT) | zod 4.6.5 (2026-09-13, MIT) or `typebox` 1.3.36 (2026-10-06, MIT; older name `@sinclair/typebox` 0.34.52) | Via OpenAPI tool below, or hand-written |
| OpenAPI production | Code-first: generated from route signatures + Pydantic models, emits 3.1.0; export with `app.openapi()` without running a server | Code-first: `@fastify/swagger` 9.9.2 from route JSON Schemas (TypeBox native; zod through `fastify-type-provider-zod` 7.0.0), or `@hono/zod-openapi` 1.6.3 | Code-first: huma v2.39.1 (2026-07-29). Spec-first: oapi-codegen v2.8.0 (2026-07-17) or ogen v1.24.0 (2026-08-07). OpenAPI 3.1 coverage of each — UNVERIFIED |
| HTTP client | httpx2 2.13.1 (see 2.3) | Built-in `fetch` (bundled undici); standalone undici 8.11.2 requires Node >= 22.19 | stdlib `net/http` |
| Mocking outbound HTTP | `MockTransport` built into httpx/httpx2 (confirmed locally); respx 0.23.1 and pytest-httpx 0.36.2 exist but only for legacy `httpx` (see 2.3) | undici `MockAgent`; msw 3.0.2 (Node >= 22.12, 12 deps); nock 15.0.1 | stdlib `net/http/httptest` |
| Structured logging | structlog 26.1.0 (MIT OR Apache-2.0, zero deps) or stdlib `logging` + own JSON formatter; python-json-logger 4.2.0 exists | pino 10.4.0 (2026-10-02, MIT, 11 deps; already a Fastify dependency) | stdlib `log/slog` |
| SQLite access | stdlib `sqlite3` | `node:sqlite` (Stability 1.2 Release Candidate only from v25.7.0; experimental in 22 and 24, no flag since 22.13) or better-sqlite3 13.0.3 (native addon, Node >= 22) | modernc.org/sqlite v1.60.1 (pure Go) or mattn/go-sqlite3 v1.14.52 (cgo) |
| Base image, compressed amd64 (Docker Hub) | python:3.14-slim 43.5 MB; python:3.14-alpine 20.4 MB | node:22-slim 79.8 MB; node:22-alpine 60.7 MB; node:24-alpine 61.6 MB | Build on golang:1.27-alpine 75.2 MB; ship a static binary on scratch/distroless (alpine:3 is 3.8 MB). Final size — UNVERIFIED estimate 10–20 MB |
| App dependency footprint | 21 installed distributions for the runtime set, 21 MB on disk (measured, Windows venv) | Fastify: 15 direct deps; transitive count not measured — UNVERIFIED. Hono: 0 | 1–3 modules beyond stdlib |
| Advisories since 2024-10 (OSV, distinct) | FastAPI 0; Starlette 8; Pydantic 0 new; uvicorn 0; httpx 0; httpx2 5; anyio 3; h11 1 — all fixed in the versions listed here | Fastify 12; undici 33 (many WebSocket/cache/proxy paths this service would not use); Hono 31+ (mostly optional middleware); pino 0 — all fixed in the versions listed | Go stdlib 75 entries in the window (covers the whole toolchain); huma 0; oapi-codegen 2; modernc sqlite 0 |
| Local toolchain | Python 3.14 + uv present | Node 22 present | Absent; Docker-only builds, no local test/lint loop |

Image sizes could not be measured by building: the Docker daemon was not running on the dev machine during
research. Final image sizes are therefore base-image figures plus estimates — **UNVERIFIED**.

### 2.3 Python 3.14 compatibility of candidate (a) — verified

Installed with uv into a Python 3.14.0 venv and exercised (FastAPI app, TestClient, mocked outbound call,
JSON logging) with warnings promoted to errors:

- pydantic-core 2.50.0 publishes `cp314` (and `cp315`) wheels; installs without a compiler.
- uvloop 0.23.0 and httptools 0.9.0 publish `cp314` wheels (Linux/macOS; uvloop has no Windows build —
  long-standing, UNVERIFIED for this release). Neither is needed at this volume.
- FastAPI 0.143.0, Starlette 1.7.0, uvicorn 0.54.0, anyio 4.15.1, structlog 26.1.0, prometheus-client 0.26.0,
  pytest 9.1.1, ruff 0.17.0, mypy 2.4.0 all declare Python 3.14 support and imported/ran cleanly.
- Local stdlib `sqlite3` reports SQLite 3.50.4, threadsafety 3 (serialized). The SQLite version inside
  `python:3.14-slim` was not checked — UNVERIFIED (the feature floor needed is 3.35 for `RETURNING`).

**HTTP client finding (important, changes the usual default):**

- `httpx` latest stable is still **0.28.1 (2024-12-06)**; only `1.0.dev` pre-releases since (latest
  1.0.dev6, 2026-08-31). Last commit on the default branch seen: 2026-02-23. Trove classifiers stop at 3.12.
  Secondary sources report the maintainer closed issues/discussions in 2026-02; a pinned discussion titled
  "Closing off access" is visible on the repo. Zero advisories in the window.
- **`httpx2` 2.13.1 (2026-09-23, BSD-3-Clause, py>=3.10, classifiers to 3.15)**, repo `pydantic/httpx2`
  (created 2026-05-11). README: a continuation of HTTPX under Pydantic stewardship. Forked from httpx 0.28.1;
  import name `httpx2`; depends on `httpcore2` (exact pin), `anyio`, `idna`, `truststore`, `h11`. 18 releases
  in 5 months. Five advisories published 2026-09-08 (SOCKS/WebSocket TLS, SSE buffering, multipart header
  injection, CL/TE conflict, decompression memory), all fixed by 2.12.0.
- Starlette 1.7.0's `TestClient` imports `httpx2` first and emits
  `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead`
  when only `httpx` is present (observed locally). FastAPI issue/PR #16358 "Migrate httpx to httpx2" closed
  2026-09-15.
- Mocking libraries have not followed yet: respx 0.23.1 depends on `httpx`; respx issues #317 "Support httpx2
  via httpcore2" and #324 are open; pytest-httpx 0.36.2 pins `httpx==0.28.*`, its issue #239 "Add support for
  httpx2" is open. A third-party `pytest-httpx2` 1.0.0 exists on PyPI — provenance not examined, UNVERIFIED,
  not recommended.
- `httpx2.MockTransport` works (confirmed locally, including through FastAPI's TestClient), so no mocking
  dependency is required.
- Another fork, `httpxyz` 0.42.1 (Codeberg), also exists; not evaluated further.

Other notes for (a):

- FastAPI 0.143.0 hard-depends on `opentelemetry-api>=1.44.0` (PyPI metadata; installed as a transitive
  dependency locally). Release notes: native OpenTelemetry support arrived in 0.142.0 (2026-09-29); automatic
  exporter setup became opt-in in 0.143.0. FastAPI is still 0.x with breaking changes flagged in several
  recent minors — pin exactly and upgrade deliberately.
- Litestar 2.24.0 has 17 unconditional dependencies (httpx, msgspec, polyfactory, rich, rich-click, pyyaml,
  multidict, …) versus FastAPI's 6; last release is four months old with 3.0 pending. Not preferred.

### 2.4 Recommendation

**(a) Python 3.14 + FastAPI + Pydantic v2 + httpx2 + uvicorn.**

Decisive reasons: the only candidate with a complete local toolchain already verified working end to end on
the installed interpreter; OpenAPI 3.1 falls out of the code with request validation from the same models;
smallest advisory surface of the two locally runnable options; workload is I/O-bound and tiny, so runtime
speed is irrelevant.

Honest trade-offs:

- Go would give the smallest image, a single static binary, and the fewest third-party dependencies (router,
  HTTP client, logging, test server all stdlib). It loses only because there is no local toolchain: every
  test/lint cycle would go through Docker.
- Node 22 is installed and Fastify/pino/undici are a solid fit, but the SQLite story is weaker (`node:sqlite`
  not yet stable on 22/24, or a native addon) and the framework/client advisory churn is the highest of the
  three.
- Python costs: FastAPI still 0.x and now pulls `opentelemetry-api`; the HTTP client ecosystem is mid-move
  from `httpx` to `httpx2` (young package name, but maintained by the Pydantic organisation and already
  adopted by Starlette). Fallback if httpx2 is rejected: `httpx` 0.28.1 is API-compatible (import swap), at
  the price of an unmaintained-looking dependency and a Starlette deprecation warning in tests.
- Largest base image of the realistic minimal options except Node (43.5 MB compressed for slim).

---

## Part 3 — Persistence and job execution

### 3.1 Storage

| | SQLite (WAL, named volume) | PostgreSQL | Redis |
|---|---|---|---|
| Extra container | None | Yes (postgres:18-alpine 120 MB compressed; PostgreSQL 18.6 current) | Yes (redis:8-alpine 39 MB; Redis 8.10.2 current) |
| Durability | Full with `synchronous=FULL`; file on a volume | Full | Depends on AOF/RDB config; not a system of record by default |
| Concurrency | One writer, many readers; ample for hundreds of posts/day | Many writers, `SKIP LOCKED` for queues | Single-threaded commands |
| Transactional "record + idempotency key + job" in one commit | Yes | Yes | Awkward (Lua/MULTI) |
| Backup | Copy/`VACUUM INTO`/`.backup` of one file | pg_dump | RDB copy |
| Ops burden | None | Credentials, upgrades, healthchecks | Persistence tuning |
| Limits | Single host only: WAL uses a shared-memory `-shm` file and does not work on network filesystems; keep `-wal`/`-shm` with the DB file; long read transactions block checkpoints | None relevant | Memory-bound |

### 3.2 Job execution

| | In-process worker polling a job table | Celery 5.6.3 / RQ 2.12.0 / arq 0.28.0 | BullMQ 6.3.12 (Node) | Temporal (temporalio 1.34.0) |
|---|---|---|---|---|
| Extra infrastructure | None | Redis or RabbitMQ + separate worker process | Redis | Temporal server + its database |
| Survives restart | Yes (rows) | Yes if broker persists | Yes if Redis persists | Yes |
| At-least-once | Yes, with lease + reclaim | Yes (acks-late style settings) | Yes | Yes (stronger) |
| Atomic with publication record | Yes, same transaction | No (dual write DB + broker) | No | N/A |
| Complexity | ~150 lines owned code | Framework, serialisation, broker ops | Framework + Redis | Highest |

Celery's classifiers stop at Python 3.13 (3.14 support UNVERIFIED). arq pins `redis<6`.

### 3.3 Recommendation

**One SQLite database file (WAL) on a named Docker volume, and an in-process asyncio worker loop that polls a
`jobs` table. No broker, no task-queue library, one container.**

Minimum design that survives restarts and gives at-least-once execution:

- Tables: `publications` (lifecycle state, request snapshot, result/error, attempts), `idempotency_keys`
  (key UNIQUE, payload fingerprint, publication id, created_at, expires_at), `jobs` (publication id, step,
  `status`, `attempts`, `run_after`, `lease_until`, `last_error`).
- Accepting a request = one transaction inserting idempotency key + publication + first job. The UNIQUE
  constraint is the race arbiter for concurrent duplicates.
- Claim = `BEGIN IMMEDIATE; UPDATE jobs SET status='running', lease_until=?, attempts=attempts+1 WHERE id =
  (SELECT id FROM jobs WHERE status='queued' AND run_after<=? ORDER BY run_after LIMIT 1) RETURNING …`.
- Crash recovery: jobs whose `lease_until` has passed are re-queued on startup and periodically.
- Retry: on retryable upstream errors set `run_after = now + backoff` (honour upstream `Retry-After`), cap
  attempts, then `failed_permanent`.
- Poll every 1–2 s plus an in-process `asyncio.Event` wake-up on insert.
- At-least-once means a crash between "upstream accepted" and "row committed" can repeat an upstream call.
  Persist intermediate upstream identifiers before the next step (e.g. the Instagram container id before
  publishing it) so a retry resumes rather than restarts. Telegram sends have no upstream idempotency key
  (to be confirmed by the Telegram slice), so a duplicate message in that narrow window is possible; record
  it as a known limit.
- Pragmas: `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=5000`, `foreign_keys=ON`.
- Deployment: named volume with the local driver (a local filesystem inside the Linux VM/host). Avoid a
  Windows bind mount for the database directory, since WAL needs shared memory and proper locking
  (inference from the SQLite WAL restrictions; not tested).
- Single replica only. If a second replica or a second writer process is ever needed, that is the point to
  move to PostgreSQL.

Library: **stdlib `sqlite3`** (Python 3.14; no install, no advisories to track beyond CPython). Use
`autocommit=True` plus explicit `BEGIN IMMEDIATE`/`COMMIT`, or the PEP 249 mode `autocommit=False` (attribute
available since 3.12; the docs recommend it over legacy `isolation_level`). Pass keyword arguments to
`connect()` — positional use beyond the first is deprecated and becomes an error in 3.15. Queries are
sub-millisecond at this size; run them through one dedicated connection via `asyncio.to_thread` (or call them
inline) rather than adding a driver.

- `aiosqlite` 0.22.1 (2025-12-23, MIT, zero deps, py>=3.9, no advisories) is the acceptable alternative if an
  awaitable API is preferred; it is a thread wrapper around the same stdlib module. Works on 3.14 (confirmed).
- SQLAlchemy 2.1.4 (2026-10-07, MIT, py>=3.11) and Alembic 1.20.0 (2026-09-11, MIT; needs SQLAlchemy + Mako):
  not warranted for three tables.

Migration tool: **not warranted.** Use numbered `.sql` files applied at startup inside a transaction, tracked
with `PRAGMA user_version`. Revisit if the schema passes roughly ten tables or moves to PostgreSQL.

---

## Part 4 — Dependency table for the recommended stack

Base: `python:3.14-slim` (Python 3.14.8). Package manager: uv (latest 0.13.0, 2026-10-09, MIT OR Apache-2.0;
dev machine has 0.11 — fine, lockfile format compatibility between them UNVERIFIED).

### 4.1 Runtime

| Package | Version | Released | Licence | Maintainer | Python | Advisories since 2024-10 | Why this, not hand-rolled / an alternative |
|---|---|---|---|---|---|---|---|
| fastapi | 0.143.0 | 2026-10-08 | MIT | FastAPI org (S. Ramírez) | >=3.10, classifiers to 3.14 | None | Routing + validation + OpenAPI 3.1 from one set of models; hand-rolling OpenAPI drifts. Litestar is heavier (17 deps) and mid-transition to 3.0 |
| starlette (transitive) | 1.7.0 | 2026-09-23 | BSD-3-Clause | Kludex / encode | >=3.10, to 3.15 | 8 (CVE-2024-47874, CVE-2025-54121, CVE-2025-62727, CVE-2026-48710, -48817, -48818, -54282, -54283); all fixed <= 1.3.1 | Required by FastAPI |
| pydantic | 2.14.0 | 2026-10-08 | MIT | Pydantic | >=3.10, to 3.15 | None new (one 2026 re-publication of CVE-2024-3772, fixed in 2.4.0) | Request/response/config validation and JSON Schema; required by FastAPI |
| pydantic-core (transitive) | 2.50.0 | 2026-10-08 | MIT | Pydantic | cp310–cp315 wheels | None | Compiled core; cp314 wheels present |
| uvicorn | 0.54.0 | 2026-09-25 | BSD-3-Clause | Kludex / encode | >=3.10, to 3.14 | None | ASGI server; plain install (h11 + click). `uvicorn[standard]` extras (uvloop, httptools, watchfiles, websockets) not needed. Granian 2.8.4 is a compiled alternative with no benefit here |
| httpx2 | 2.13.1 | 2026-09-23 | BSD-3-Clause | Pydantic (pydantic/httpx2) | >=3.10, to 3.15 | 5 (CVE-2026-84378 … -84382, published 2026-09-08); fixed <= 2.12.0 | Async client with timeouts, connection pooling, multipart and a built-in `MockTransport`. Stdlib `urllib` is sync and has no pooling; aiohttp 3.14.4 adds 7 more dependencies. Legacy `httpx` 0.28.1 has had no stable release since 2024-12 |
| httpcore2, h11, anyio, idna, truststore (transitive) | 2.13.1 / 0.16.0 / 4.15.1 / 3.20 / 0.10.4 | 2026-09-23 / 2025-04-24 / 2026-09-05 / 2026-09-17 / 2025-08-12 | BSD-3 / MIT / MIT / BSD-3 / MIT | — | all install on 3.14 | h11 CVE-2025-43859 (fixed 0.16.0); anyio CVE-2026-63349, -63374, -64847 (fixed 4.14.2); idna CVE-2026-45409 (fixed 3.15) | Pulled by httpx2 / Starlette / uvicorn |
| opentelemetry-api, typing-extensions, typing-inspection, annotated-types, annotated-doc, click (transitive) | 1.45.1 / 4.16.0 / 0.4.4 / 0.8.0 / 0.0.5 / 8.5.0 | 2026-10-06 / 2026-07-02 / 2026-08-12 / 2026-07-23 / 2026-07-28 / 2026-08-26 | Apache-2.0 / PSF-2.0 / MIT / MIT / MIT / BSD-3 | — | — | None | Pulled by FastAPI / Pydantic / uvicorn |
| structlog | 26.1.0 | 2026-06-06 | MIT OR Apache-2.0 | Hynek Schlawack | >=3.10, to 3.15 | None | JSON logs with bound context (publication id, trace id) via contextvars; zero deps. Stdlib `logging` + a custom JSON formatter is a viable zero-dependency fallback |
| prometheus-client | 0.26.0 | 2026-07-24 | Apache-2.0 AND BSD-2-Clause | Prometheus project | >=3.9, to 3.14 | None | Official client; correct exposition format and histograms; zero deps |
| sqlite3 | stdlib | — | PSF | CPython | 3.14 | — | See Part 3 |

Runtime closure measured locally: 21 distributions, 21 MB (with pydantic-settings and python-dotenv, which
the table above omits — see below).

### 4.2 Dev / test

| Package | Version | Released | Licence | Maintainer | Python | Advisories since 2024-10 | Why |
|---|---|---|---|---|---|---|---|
| pytest | 9.1.1 | 2026-06-19 | MIT | pytest-dev | >=3.10, to 3.15 | CVE-2025-71176 (tmpdir handling; fixed 9.0.3) | Test runner |
| anyio pytest plugin | ships with anyio 4.15.1 | — | MIT | A. Grönholm | — | see anyio | Async tests without another package. Alternative: pytest-asyncio 1.4.0 (2026-05-26, Apache-2.0) |
| HTTP mocking | `httpx2.MockTransport` (built in) | — | — | — | — | — | respx / pytest-httpx do not support httpx2 yet; the built-in transport covers status/headers/body assertions |
| ruff | 0.17.0 | 2026-10-09 | MIT | Astral | any | None | Lint + format in one binary |
| mypy | 2.4.0 | 2026-10-01 | MIT | python/mypy | >=3.10, to 3.15 | None | Type check; Pydantic ships a mypy plugin. Alternative: pyright 1.1.414 (needs Node) |
| pytest-cov (optional) | 7.1.0 | 2026-03-21 | MIT | pytest-dev | >=3.9 | None | Coverage, only if a threshold is enforced |

### 4.3 Deliberately not added

| Not added | Why |
|---|---|
| Vendor SDKs (python-telegram-bot, aiogram, facebook-business, LinkedIn clients) | The three APIs are plain HTTPS + JSON/multipart; SDKs bring their own HTTP stacks, large transitive trees and release lag, and hide the retry/rate-limit behaviour this service must own |
| ORM / SQLAlchemy / Alembic | Three tables, hand-written SQL; `PRAGMA user_version` migrations |
| Task queues (Celery, RQ, arq, Dramatiq, Huey, Temporal) and Redis / RabbitMQ / NATS | Need a broker or server; break the single-transaction accept; job table + loop is enough |
| tenacity 9.2.1 (or any retry library) | Retries here are persisted state transitions (`attempts`, `run_after`) that must survive restarts, not in-memory decorators; in-memory retry would also hide attempts from the publication record |
| pydantic-settings 2.15.0 (+ python-dotenv) | A dozen environment variables can be validated by a plain Pydantic model built from `os.environ`; Compose supplies env files. One advisory each in 2026 (CVE-2026-58203; CVE-2026-28684). Reasonable to add later if configuration grows |
| python-multipart 0.0.32 | Only needed for inbound form/multipart parsing; the API is JSON with media by URL. 8 distinct advisories since 2024-10 |
| uvloop 0.23.0, httptools 0.9.0, orjson 3.13.0 | Performance extras with native code; irrelevant at this volume |
| `fastapi[standard]` extra | Pulls fastapi-cli, jinja2, email-validator, OpenTelemetry SDK/exporter, legacy httpx, python-multipart |
| respx 0.23.1, pytest-httpx 0.36.2 | Tied to legacy `httpx` |
| OpenTelemetry SDK/exporters | No collector in a single-operator Compose deployment; `traceparent` is logged instead. `opentelemetry-api` arrives transitively and stays a no-op |
| gunicorn / multiple workers | A single process is required anyway (one SQLite writer, one worker loop) |
| schemathesis 4.30.1 | Useful contract fuzzing but 14 direct dependencies; revisit if the API gets external consumers |

---

## UNVERIFIED items (collected)

- Final Docker image sizes for all three candidates (daemon not running; only base-image compressed sizes read from Docker Hub).
- SQLite library version inside `python:3.14-slim`.
- Node 22 end-of-life date; Fastify transitive dependency count.
- Go tools' OpenAPI 3.1 coverage (huma, oapi-codegen, ogen) and their licences; Go image size estimate.
- gRPC default message size limit; exact RFC 9110 wording for 202; W3C Trace Context Level 2 status.
- Tooling support for OpenAPI 3.2.
- Exact reason/date for the httpx maintainer closing issues (secondary sources only; pinned discussion title seen on the repo).
- Provenance of the third-party `pytest-httpx2` package.
- Celery support for Python 3.14; uvloop Windows support; uv 0.11 ↔ 0.13 lockfile compatibility.
- Telegram and Instagram media/idempotency behaviours referenced here belong to the other research slices.
- Named-volume vs bind-mount behaviour of SQLite WAL under Docker Desktop on Windows (inference, not tested).

---

## Sources consulted

Registry / API endpoints (queried on 2026-10-09):

- https://pypi.org/pypi/{package}/json for: fastapi, starlette, litestar, pydantic, pydantic-core, pydantic-settings, httpx, httpcore, h11, anyio, uvicorn, uvloop, httptools, structlog, prometheus-client, pytest, pytest-asyncio, respx, pytest-httpx, ruff, mypy, pyright, aiosqlite, sqlalchemy, alembic, python-multipart, tenacity, granian, orjson, typing-extensions, annotated-types, idna, certifi, click, sniffio, typing-inspection, pytest-cov, coverage, celery, rq, arq, saq, procrastinate, temporalio, dramatiq, huey, aiohttp, niquests, msgspec, python-json-logger, uv, hatchling, pytest-xdist, time-machine, freezegun, yoyo-migrations, opentelemetry-api, annotated-doc, schemathesis, httpx2, httpxyz, httpcore2, pytest-httpx2, truststore, psycopg, asyncpg, redis
- https://registry.npmjs.org/{package} for: fastify, hono, zod, @sinclair/typebox, typebox, undici, pino, better-sqlite3, msw, nock, vitest, typescript, @fastify/swagger, @hono/node-server, @hono/zod-openapi, bullmq, prom-client, fastify-type-provider-zod
- https://proxy.golang.org/{module}/@latest for: github.com/oapi-codegen/oapi-codegen/v2, github.com/danielgtaylor/huma/v2, github.com/ogen-go/ogen, modernc.org/sqlite, github.com/mattn/go-sqlite3, github.com/prometheus/client_golang
- https://go.dev/dl/?mode=json
- https://nodejs.org/dist/index.json
- https://endoflife.date/api/python.json, /nodejs.json, /postgresql.json, /redis.json, /valkey.json, /sqlite.json
- https://api.osv.dev/v1/query (ecosystems PyPI, npm, Go; packages listed in Parts 2 and 4)
- https://hub.docker.com/v2/repositories/library/{python,node,golang,alpine,debian,postgres,redis}/tags/{tag}
- https://datatracker.ietf.org/api/v1/doc/document/?name=draft-ietf-httpapi-idempotency-key-header
- https://datatracker.ietf.org/api/v1/doc/document/?name=draft-ietf-httpapi-ratelimit-headers
- https://datatracker.ietf.org/api/v1/doc/document/?name=rfc9457
- https://datatracker.ietf.org/api/v1/doc/state/
- https://api.github.com/repos/encode/httpx (+ /commits, /releases)
- https://api.github.com/repos/pydantic/httpx2
- https://api.github.com/search/issues (repos lundberg/respx, Colin-b/pytest_httpx, fastapi/fastapi; query "httpx2")

Pages:

- https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/
- https://www.ietf.org/archive/id/draft-ietf-httpapi-idempotency-key-header-07.html
- https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/
- https://www.rfc-editor.org/rfc/rfc9457.html
- https://www.w3.org/TR/trace-context/
- https://google.aip.dev/151
- https://spec.openapis.org/oas/latest.html
- https://connectrpc.com/docs/protocol/
- https://fastapi.tiangolo.com/release-notes/
- https://fastapi.tiangolo.com/how-to/extending-openapi/
- https://docs.litestar.dev/latest/release-notes/changelog.html
- https://docs.python.org/3.14/library/sqlite3.html
- https://www.sqlite.org/wal.html
- https://nodejs.org/api/sqlite.html
- https://nodejs.org/en/about/previous-releases
- https://pkg.go.dev/net/http#ServeMux
- https://go.dev/doc/devel/release
- https://github.com/encode/httpx/discussions
- https://github.com/pydantic/httpx2
- https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md
- Web search result (secondary, used only for the httpx maintenance context): https://docs.bswen.com/blog/2026-03-05-httpx-library-status

Local verification: `uv venv --python 3.14` + install of the Part 4 set; smoke script exercising FastAPI
TestClient, `httpx2.MockTransport`, `sqlite3`, `aiosqlite`, structlog JSON output, with warnings as errors.
