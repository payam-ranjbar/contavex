# Requirements

Normalized from the project brief. Each requirement has an ID so research
notes, architecture decisions and tests can refer to it. Where research
changes a requirement, the change is recorded in the "Revisions" section at
the bottom rather than silently edited.

Status: **implemented 2026-10-09** for all three platforms against faked vendor APIs. Not yet run against real vendor accounts (no credentials were available).

## 1. Purpose

A standalone, Dockerized service that publishes and manages social-media
content on several external platforms. Other applications and containers
call one stable network contract; they never deal with vendor APIs, vendor
authentication, SDKs, upload procedures or vendor quirks.

Platform priority:

1. Telegram (channel publishing through the official Bot API) — implemented first, as the simplest reference
2. Instagram (current official Meta API) — the business priority
3. LinkedIn — only after 1 and 2 work and their automated tests pass

## 2. Actors

| Actor | Description |
|---|---|
| Consumer | Another application/container that submits publishing requests over the network. Holds no vendor credentials. |
| Operator | The person/team running this service. Owns the vendor accounts, apps and credentials. Single operator; not a public SaaS. |
| Provider | An external platform API (Telegram, Meta, LinkedIn). |

## 3. Functional requirements

### Publishing contract

| ID | Requirement |
|---|---|
| F-1 | A consumer can submit a publishing request naming a target account, with text, media attachments, optional title/caption, post type where relevant, optional platform-specific options, and correlation/idempotency information. |
| F-2 | The service, not the consumer, maps the request onto the target provider's API. |
| F-3 | The contract exposes a coherent common model **and** platform-specific capabilities. It must not collapse to the lowest common denominator. |
| F-4 | A request for something the target cannot do is rejected explicitly (validated before any provider call where possible), never silently degraded. |
| F-5 | Consumers can discover what each configured target supports (capability discovery). |
| F-6 | The contract can evolve without breaking existing consumers (versioning strategy). |
| F-7 | Where the provider permits, published content can be edited and deleted through the same contract; where it does not, that is reported as an unsupported capability. |

### Integrations

| ID | Requirement |
|---|---|
| F-10 | A logical platform (`telegram`, `instagram`, ...) may have more than one implementation. Switching implementation is an operator configuration change and does not change the consumer contract. |
| F-11 | Telegram: publish to a channel via a bot that is a channel administrator with only the rights needed. Official Bot API only — no scraping, browser automation or user-account automation. |
| F-12 | Telegram coverage to research and support as MVP permits: text, images, video, documents, albums, captions, formatting, editing, deletion, channel targeting. |
| F-13 | Instagram: publish to operator-controlled accounts through the currently supported official Meta API, handling the real media lifecycle (images, video/Reels, carousels, processing states). |
| F-14 | Instagram: no multi-tenant OAuth onboarding unless research shows it is required for operator-owned accounts. Document what that would take later. |
| F-15 | LinkedIn: fits the same contract where practical; fundamental differences are represented explicitly. Implemented after the Phase D architecture review. |
| F-16 | Adding an account, a platform, or a second implementation of a platform are documented, bounded tasks. |

### Lifecycle, idempotency, reliability

| ID | Requirement |
|---|---|
| F-20 | A caller can always determine whether a publication was accepted, is processing, was published, or failed — and whether a failure is retryable or permanent. |
| F-21 | The contract does not pretend an operation is synchronous when the provider processes it asynchronously. |
| F-22 | A caller retry of the same logical request must not create a duplicate post, to the extent the provider makes that preventable. The limits of the guarantee are documented per provider. |
| F-23 | Failure handling distinguishes: transient, permanent, authentication, expired token, rate limit, upstream 5xx, media-processing failure, malformed request, unsupported operation, timeout. |
| F-24 | Retry/backoff policy is per failure class and per provider (honouring provider-supplied retry hints). Permanent errors are never retried indefinitely. |
| F-25 | Errors returned to consumers are normalized: one error vocabulary regardless of provider, with the provider detail available but sanitized. |

## 4. Non-functional requirements

### Security

| ID | Requirement |
|---|---|
| S-1 | Credentials and tokens never appear in: Git, Docker image layers, application source, API responses, health endpoints, exception output, logs, traces, fixtures, test snapshots. |
| S-2 | Secret injection mechanism is chosen from research for each of: local dev, Docker Compose, CI, production. Environment variables are not assumed to be the answer. |
| S-3 | Business logic obtains secrets through one seam so that a dedicated secret manager can be added later without rewriting it. |
| S-4 | Documented: secret lifecycle, injection, access, redaction, rotation, failure behaviour, least privilege. |
| S-5 | Example configuration contains placeholders only. |
| S-6 | Consumers cannot obtain vendor credentials through the service. |

### Service interface

| ID | Requirement |
|---|---|
| I-1 | Network-facing interface chosen from research (interoperability, simplicity, schema, validation, generated clients, async operations, media transfer, observability, retries, backwards compatibility) and justified. |
| I-2 | Machine-readable interface description is published where the technology supports it. |
| I-3 | Consumers never import the service's internal integration code. |

### Observability

| ID | Requirement |
|---|---|
| O-1 | Structured logs; request correlation IDs; publication IDs; upstream request IDs when the provider supplies them; retry events; sanitized errors. |
| O-2 | Liveness and readiness checks. |
| O-3 | Metrics if they earn their keep. |
| O-4 | Complete provider requests are not logged when they carry credentials or sensitive data. |

### Delivery

| ID | Requirement |
|---|---|
| D-1 | Runs through Docker: reproducible build, minimal production image, non-root, health/readiness, no secrets in layers. |
| D-2 | Persistence, if needed, is intentional (named volume, documented). |
| D-3 | Another container can reach the service; internal networking is documented. |
| D-4 | A new developer can, from a clean clone: build, configure, supply dev secrets safely, run tests, start the service, submit a test request, add an account, add a platform, add a second implementation. |

### Engineering constraints

| ID | Requirement |
|---|---|
| E-1 | No language, framework, database, queue, SDK, pattern or module structure is assumed before research. The architecture is derived from requirements + research and the choice is justified against alternatives. |
| E-2 | Every important dependency is researched from current primary sources: stable version, maintenance, release recency, supported runtimes, advisories, official or not, whether it is needed at all, transitive footprint, license. Official SDK vs third-party SDK vs direct API is compared. |
| E-3 | Prefer the least complex architecture that satisfies the requirements and leaves clean extension points. No ceremony. |
| E-4 | Findings and decisions are persisted in the repository as work proceeds. |

## 5. Testing requirements

| ID | Requirement |
|---|---|
| T-1 | Core: request validation, integration selection, unsupported platform, unsupported capability, configuration, error normalization. |
| T-2 | Per provider implementation: success, auth failure, invalid content, rate limit, temporary upstream failure, permanent upstream failure, timeout, malformed upstream response, media failure, retry behaviour. |
| T-3 | Contract tests: one behavioural suite executed against every implementation that claims a capability. |
| T-4 | Docker: clean build, container starts, health/readiness, safe config injection, no secrets in image, reproducible test run, reachable from another container. |
| T-5 | Optional live-provider tests that cannot run by accident. |
| T-6 | Suites are clearly separated: unit / integration (mocked provider) / contract / docker-smoke / live. |

## 6. Out of scope

Unless an immediate technical requirement justifies it: user-facing
dashboard, AI copywriting, AI agents, campaign planner, analytics dashboard,
social inbox, CRM, billing, multi-tenant SaaS control plane.

Also out of scope by decision of the brief: scraping, browser automation,
user-account automation.

## 7. Process and gates

```
INGEST -> RESEARCH -> ARCHITECT -> FOUNDATION -> TELEGRAM -> INSTAGRAM
       -> ARCHITECTURE REVIEW -> LINKEDIN -> HARDEN+TEST -> VERIFY DOCKER -> DOCUMENT
```

| Gate | Condition to pass |
|---|---|
| Research -> Architect | Platform research is sufficient to make informed decisions; `docs/RESEARCH.md`, `DEPENDENCIES.md`, `SECURITY.md`, `INTEGRATIONS/*.md` exist. |
| Architect -> Foundation | Alternatives compared, decision recorded with reasons, operator has reviewed it. |
| Instagram -> LinkedIn | Telegram and Instagram functional, automated tests green, Phase D review written, LinkedIn research refreshed. |

## 8. Questions the research must answer

These are the facts the architecture depends on. Answers are recorded in
`RESEARCH.md` and the integration documents.

1. Which publish operations are synchronous at the provider and which are not? (drives F-20/F-21 and whether a background worker and persistence are needed at all)
2. How does media reach each provider — bytes uploaded by us, or a URL the provider fetches? Must that URL be public? (drives the media-transfer design of the consumer contract)
3. Does any provider offer an idempotency key or a way to detect an already-created post after a lost response? (bounds F-22)
4. Which credentials are static and which are refreshed/rotated at runtime? (drives S-2/S-3: read-only injection vs a writable token store)
5. What are the documented rate limits and retry hints per provider? (drives F-24)
6. What can be edited/deleted after publishing, per provider? (drives F-7)
7. What approvals/reviews does each provider require before an operator can publish to their own account? (drives what can be live-tested, and the Definition of Done for LinkedIn)

## 9. Assumptions (to be confirmed with the operator)

- A-1: One operator, a handful of accounts per platform, low volume (tens to hundreds of posts per day).
- A-2: Consumers are trusted workloads on a private container network, but are still authenticated to the service so that network reachability alone does not grant the ability to publish.
- A-3: Deployment target is Docker Compose on a single host initially; nothing in the design should prevent an orchestrator later.
- A-4: Scheduling posts for a future time is a consumer concern (campaign planner is out of scope); the service publishes on request.

## Revisions

| Date | Change | Reason |
|---|---|---|
| 2026-10-09 | **F-26 added:** consumers upload media files to the service; the service is responsible for getting them to each provider in the form that provider needs. | Operator: media originates in Contavex and is not publicly hosted. Research: Instagram needs a public URL; LinkedIn and Telegram need bytes. |
| 2026-10-09 | **F-27 added:** making media publicly reachable ("staging") is a replaceable strategy — at least object storage/CDN and self-hosting — independent of the provider that needs it. | Operator decision. |
| 2026-10-09 | **F-20 extended:** a publication can also end in *outcome unknown*. | Research: no provider offers idempotency keys; some lost responses cannot be reconciled. |
| 2026-10-09 | **S-2 extended:** credentials that change at runtime are stored encrypted in a writable store, separate from injected secrets. | Research: Instagram and LinkedIn tokens are refreshed. Operator chose encryption. |
