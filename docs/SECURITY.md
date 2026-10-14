# Security

Researched: **2026-10-09**. Sources and the full mechanism comparison:
[research/docker-and-secrets.md](research/docker-and-secrets.md).

Status: implemented. Each mechanism below names the test that holds it in
place where there is one.

## What is being protected

| Secret | Kind | Changes at runtime? |
|---|---|---|
| Telegram bot token | Static bearer credential, embedded in the request URL path | No |
| Meta app secret | Static | No |
| Instagram long-lived access token | Bearer, 60 days | **Yes** — refreshed by the service |
| LinkedIn client secret | Static | No |
| LinkedIn access / refresh tokens | Bearer, 60 / 365 days | **Yes** — obtained by interactive consent, refreshed where permitted |
| Consumer API keys | Authenticate callers to this service | No |
| Credential-store master key | Encrypts the mutable tokens at rest | No |

Anyone holding one of the vendor credentials can publish as the operator.

## Rules

Secrets never appear in: Git, image layers, source code, API responses,
health endpoints, exception output, logs, traces, fixtures or test snapshots
(REQUIREMENTS S-1).

## How secrets get in

### Decision: mounted files, not environment variables

Docker's Compose documentation warns that environment variables "are often
available to all processes" and "can also be printed in logs when debugging
errors without your knowledge", and recommends Compose secrets. OWASP's
Secrets Management and Cryptographic Storage cheat sheets say the same and
recommend a mounted file. Compose grants a file secret only to services that
list it, and it never enters the container's environment.

So: **environment variables carry non-secret settings and the *path* to each
secret; the secret value is always read from a file.**

What this does and does not buy: it removes accidental exposure (environment
dumps, child-process inheritance, crash reporters, container inspection). It
does not protect against a compromised process running as the service's
user, or against someone with access to the host or the Docker daemon.
Compose file secrets are **not encrypted at rest** — only Swarm and external
secret managers are.

### Per environment

| Environment | Mechanism | Where the secret rests |
|---|---|---|
| Local development | Compose `secrets:` with `file:` sources pointing into the git-ignored `secrets/` directory. Test/sandbox vendor credentials only. | Plaintext file on the developer's disk, protected by OS file permissions |
| Docker Compose, single host | The same mechanism, one secret per credential, granted only to this service | Plaintext file on the host; restrict with host permissions |
| CI | No real vendor credentials — tests run against fakes. If a build ever needs a private feed: CI secret → `--mount=type=secret`. Actions pinned by full commit SHA. | CI secret store |
| Production | Orchestrator secret store or external manager, **still delivered as files** under `/run/secrets/` so the application is identical everywhere | Encrypted in the manager |

Never: vendor tokens in `ARG` or `ENV` (they persist in image history and in
`mode=max` provenance), or in the build context.

### The seam for secret managers (REQUIREMENTS S-3)

All secret access goes through one small interface: *resolve a named secret
to its value*. The first implementation reads a file. Provider code receives
resolved credentials and never knows where they came from. Adding Vault or a
cloud secret manager later means adding one implementation of that
interface; no publishing logic changes.

Secrets are referenced by **name** in the account configuration, never by
value:

```toml
# config/contavex.toml — non-secret, safe to commit
[accounts.news-channel]
platform       = "telegram"
implementation = "bot-api"
chat_id        = "-1001234567890"
credential     = "telegram_news_bot_token"   # a secret NAME, resolved at runtime
```

### File-reading rules

- Fail fast at startup if a referenced secret is missing, empty or unreadable. The error names the secret, never its content.
- Trim exactly one trailing `\r\n` or `\n`. A Windows editor or `echo` adds one, and the bind mount preserves it byte for byte.
- Do not enforce a file-mode check (e.g. "must be 0400"). On Docker Desktop for Windows, bind-mounted files are fixed at 0777 and Compose silently ignores `uid`/`gid`/`mode` for file-sourced secrets. Protect the host file with host permissions instead.
- No fallback from `X_FILE` to a plain `X` environment variable.

Create a secret file without a trailing newline:

```powershell
Set-Content -NoNewline -Path secrets\telegram_news_bot_token -Value '<paste token>'
```

```sh
printf '%s' '<paste token>' > secrets/telegram_news_bot_token
```

## Mutable credentials: the credential store

Instagram and LinkedIn tokens change while the service runs, so read-only
injection cannot hold them. No Docker source covers this case; the pattern
below is our design, built on OWASP's guidance to use authenticated
encryption and to keep keys apart from the data they protect.

- **Bootstrap secrets** (injected, read-only): static credentials, the *initial* token for each account, and the master key.
- **Credential store** (writable): current tokens, expiries, scopes, in the service database on a named volume. Each value is encrypted with AES-GCM under the injected master key. The key lives in the secret mount, the ciphertext on the volume — separate places.
- The injected initial token is a **seed**. Once the service has refreshed it, the stored token is the truth; a stale seed must never overwrite a newer stored token.
- **One writer.** Two instances refreshing the same token can invalidate each other. The service runs as a single replica.
- The volume is **secret-bearing and backup-critical**. Losing it means repeating the manual bootstrap (Instagram) or the browser consent (LinkedIn). Backups of the volume are ciphertext for the token columns, but the master key must be backed up separately.
- There is no re-encryption command yet. Rotating the master key means supplying fresh seed tokens; see Rotation.
- Each ciphertext is bound to its account and role, so a row copied to another account does not decrypt.

## Access inside the service

- Credentials are resolved per provider call and passed to that provider only. Telegram code cannot obtain Meta tokens.
- Credential values are wrapped in a type whose text representation is a fixed placeholder, so accidental formatting or logging prints `***`, not the value.
- No API endpoint returns a credential. Account endpoints report identity and health (`ok`, `expiring`, `reauthorization_required`, expiry date) only.
- Consumers authenticate with their own API keys, compared in constant time. Reaching the service on the network is not sufficient to publish.

## Redaction

Defence in depth, because the Telegram token sits in the URL path and Meta
tokens travel as query parameters:

1. **Do not log what cannot be safe.** Outbound requests are logged as method, host, a *route template* (e.g. `/bot<token>/sendPhoto`) and status — never the real URL, headers or body.
2. **A redaction processor** in the logging pipeline scrubs known credential values and credential-shaped patterns from every record, including exception text.
3. **Exceptions from the HTTP client** are converted to the service's own error type before they propagate, because client exceptions embed the request URL.
4. **Provider error payloads** are stored and returned only after sanitisation, and only known-safe fields (error code, message, the provider's request/trace id).
5. **Tests** inject canary credentials and assert they appear in no log line, API response, stored row or error across the success and failure paths.

### Where this is tested

| Guarantee | Test |
|---|---|
| A credential echoed back by a vendor never reaches logs, API responses or the database | `test_no_secret_leaks.py`; `test_bot_token_never_leaks`, `test_access_token_never_leaks`, `test_linkedin_credentials_never_leak` |
| Refreshed tokens are ciphertext at rest; a wrong master key refuses to decrypt | `TestTokenRefresh` in `test_instagram_login.py` |
| A stale seed never overrides a refreshed token; a new seed does | same |
| Missing secret, bad account, bad master key, bad staging config stop startup | `TestStartupFailsFast`, `TestStagingConfiguration`, provider `TestValidation` classes |
| Nothing from the build context is in any image layer; no credential in image config or container environment | `tests/docker/test_docker.py` |
| An upload URL returned by LinkedIn outside `linkedin.com` gets neither bytes nor token | `test_upload_url_outside_linkedin_is_refused` |
| The public media listener serves staged files and nothing else | `test_uploaded_image_is_public_only_while_publishing` |

## Rotation

| Credential | How | Service impact |
|---|---|---|
| Telegram bot token | BotFather `/token`; replace the secret file; restart | Posts fail with an authentication error between revocation and restart |
| Instagram token | Automatic refresh before expiry. If it lapses: generate a new one in the App Dashboard, replace the seed, mark it as newer | Account reports `reauthorization_required`; publications fail fast with a typed error |
| LinkedIn tokens | Automatic refresh where LinkedIn grants refresh tokens; otherwise browser re-consent before expiry | Same as above, with the expiry date visible ahead of time |
| Consumer API key | Add the new key, switch the consumer, remove the old key. Two keys may be valid at once. | None |
| Master key | Not automated yet. Replace the key and re-supply each account's token as a new seed (a seed that differs from the one a stored token came from always wins) | Restart; Instagram and LinkedIn tokens are re-read from their seed files |

## Failure behaviour

| Situation | Behaviour |
|---|---|
| Referenced secret missing at startup | Service refuses to start; readiness never passes |
| Provider rejects a credential | Publication fails as `authentication` (permanent, not retried); account health flips; other accounts unaffected |
| Token expired and not refreshable | Account `reauthorization_required`; new publications to it are refused immediately with that reason |
| Credential store undecryptable (wrong master key) | Service refuses to start rather than run with partial credentials |

Health and readiness endpoints never call vendor APIs and never include
credential material; a vendor outage must not mark the service unhealthy.

## Least privilege

| Area | Measure |
|---|---|
| Telegram | Bot is a channel admin with `can_post_messages` only (covers posting and deleting its own posts). One bot per channel group if blast radius matters. |
| Instagram | Scopes `instagram_business_basic` + `instagram_business_content_publish` only. |
| LinkedIn | `w_member_social` or `w_organization_social` only; read scopes added only if reconciliation needs them. |
| Container | Numeric non-root user, read-only root filesystem, `cap_drop: [ALL]`, `no-new-privileges`, writable paths limited to the data volume and a tmpfs. No Docker socket. |
| Network | No published host ports. Consumers reach the service by name on a dedicated network. |
| Image | No shell tools added for health checks (no curl/wget next to vendor credentials). |

## Media handling

- The service does not download consumer-supplied URLs. A media `url` is only
  ever handed to a provider that fetches it itself, so there is no
  server-side request forgery surface in the service.
- Uploads are size-limited, streamed to the data volume, stored under
  generated names (never a consumer-supplied path), and removed after a
  retention period.
- **Staging** makes a file public on purpose, for providers that fetch by URL
  (Instagram). Treat every staged file as published. Staged URLs are
  unguessable and withdrawn when the publication ends.
- The self-hosted stager is the only internet-facing surface. It runs on a
  separate listener that serves staged files by token and nothing else: no
  API routes, no listing, no health detail. The consumer API is never
  published to the internet.
- Object-storage credentials for the other stager are secrets like any
  other, scoped to write and delete in one bucket or prefix.

## Verifying nothing leaked into the image

Run in the Docker verification phase:

- `docker image history --no-trunc` and `docker image inspect` — no credential names or values in layer commands or image config.
- A canary build: place a fake token in the working tree, build, export the image, and assert the token is absent from every layer.
- A secret scan of the image with a pinned scanner. Note that Trivy suffered a supply-chain compromise in March 2026 (CVE-2026-33634); use a current release and pin by digest or commit SHA. Docker Scout checks vulnerabilities, not secrets.

## Example configuration

Only placeholders are ever committed:

```
secrets/
  telegram_news_bot_token.example      contains: REPLACE_WITH_BOT_TOKEN
  instagram_brand_access_token.example contains: REPLACE_WITH_LONG_LIVED_TOKEN_FROM_META_APP_DASHBOARD
  consumer_api_key_demo.example        contains: REPLACE_WITH_A_LONG_RANDOM_STRING
  master_key.example                   contains: REPLACE_WITH_32_RANDOM_BYTES_BASE64
  s3_access_key_id.example, s3_secret_access_key.example, linkedin_*.example
```

Everything else under `secrets/` is git-ignored and excluded from the Docker
build context.
