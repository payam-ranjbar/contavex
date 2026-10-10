# Research notes: Docker secret management and image build/run practice

Researched 2026-10-09. Dev machine: Windows 11, Docker CLI 29.1.2, Compose v2.40.3-desktop.1, `docker init` v1.4.0, `docker scout` v1.18.3 (versions read from the local CLI; the Docker Desktop daemon was not running, so nothing below was reproduced locally).

Conventions: "quoted text" is taken from the cited page. **UNVERIFIED** = not confirmed in a primary source opened during this research. **INFERENCE** = my reasoning from cited facts, not a statement in a source.

---

## 1. Secrets

### 1.1 Comparison table

| Mechanism | Where the secret lives at rest | Who can read it at runtime | Rotation | What the docs say | Fit |
|---|---|---|---|---|---|
| Plain env vars (`environment:`, `-e`, `env_file`, `.env`) | Plaintext in `.env`/compose file on host; in container config held by the daemon | Every process in the container ("often available to all processes"); inherited by child processes; anyone with Docker API access via container config (UNVERIFIED in docs that `docker inspect` prints `.Config.Env` - widely known, but the inspect reference page does not show it); `/proc/<pid>/environ` (UNVERIFIED in sources opened); "may be included in logs or system dumps" (OWASP) | Recreate container | Compose: "If you're injecting passwords and API keys as environment variables, you risk unintentional information exposure." Compose env best practices: "Consider using Secrets for managing sensitive information." OWASP Crypto Storage: do not store keys in environment variables | Non-secret config only |
| Compose secret, `file:` source | Plaintext file on host (must be gitignored and ACL'd by you) | Mounted read-only at `/run/secrets/<name>`; only services that list it under `secrets:`; implemented as a bind mount, default mode `0444`; not in container env | Edit file, recreate container | Docker's recommended Compose mechanism: "Using secrets mitigates these risks" | **Local dev and single-host Compose** |
| Compose secret, `environment:` source | Host env var (shell/CI) - never written to a host file by Compose | Same `/run/secrets/<name>` file; `uid`/`gid`/`mode` honoured for this source only | Change env var, recreate | Documented; "Environment secrets are not supported when deploying with `docker stack deploy`" | **CI**, and dev when the secret comes from a password manager CLI |
| Swarm secrets (`docker secret create`, `external: true`) | "stored in the Raft log, which is encrypted"; sent to managers over mutual TLS | Only nodes running tasks granted the secret; "mounted into the container in an in-memory filesystem" at `/run/secrets/<name>`; max 500 kb | Immutable: create a new versioned secret ("adding a version number or date to the secret name"), update the service, remove the old one | OWASP Docker rule #12 recommends Docker Secrets | Production on Swarm |
| Kubernetes Secret, volume mount | etcd, **unencrypted by default** ("stored unencrypted in the API server's underlying data store (etcd)") unless encryption at rest is enabled | tmpfs on the node; anyone who can create a Pod in the namespace can read any Secret in it; `list` access implies reading contents | Mounted files update automatically (kubelet sync, eventually consistent); **not** with `subPath` | K8s: enable encryption at rest, least-privilege RBAC, restrict to specific containers, consider external store providers | Production on K8s (prefer volume) |
| Kubernetes Secret, env var | Same | Same plus the env-var exposure in row 1 | Not updated until the container restarts | K8s docs treat both as valid, no explicit ranking | Avoid for these credentials |
| External secret manager (Vault, AWS/GCP/Azure, via sidecar/agent or Secrets Store CSI driver) | Encrypted in the manager; audit log; access policy | Workload identity only; sidecar writes to a shared in-memory volume | Best: central rotation, dynamic/short-lived credentials, leases | OWASP: sidecars "fetch secrets from some remote endpoint and then store them on a shared volume"; K8s: Secrets Store CSI Driver mounts external secrets as a volume | Production target state |
| Build-time secret (`RUN --mount=type=secret`, Compose `build.secrets`) | Host file/env at build time only | Only that `RUN` step; default path `/run/secrets/<id>`, mode `0400`, uid/gid 0; "Secret mounts don't leak outside of the build and are never included in provenance attestations" | n/a | "Build arguments and environment variables are inappropriate for passing secrets to your build, because they persist in the final image." | Only for private registries/package feeds; **never for vendor tokens** |
| `ARG` / `ENV` in Dockerfile | Image config and history, forever | Anyone with the image | Rebuild + revoke | "Build arguments are visible in the `docker history` command and in `max` mode provenance attestations" | Never |

### 1.2 Verdict: env vars vs mounted secret files

**Use mounted files. Env vars carry non-secret config and the *path* to the secret.**

Cited reasons:
- Compose docs: env vars "are often available to all processes, and it can be difficult to track access. They can also be printed in logs when debugging errors without your knowledge."
- OWASP Secrets Management: secrets in env "are generally accessible to all processes and may be included in logs or system dumps"; preferred alternative is to "keep our secrets within a particular config/secret file and mount that file to our instance as a mounted volume."
- OWASP Cryptographic Storage: avoid storing keys in environment variables.
- Compose grants file secrets per service ("Services can only access secrets when explicitly granted"), so a sibling container does not get them.
- Kubernetes: mounted Secrets update in place; env-var Secrets need a restart.

Limits to state honestly (INFERENCE): a file secret is still plaintext to anything running as the app's uid or with host/Docker-daemon access. It removes accidental exposure (env dumps, child process inheritance, crash reporters, `inspect`), not a compromised process. Compose secrets are not encrypted at rest; only Swarm (Raft) and external managers are.

### 1.3 The `*_FILE` convention

- Compose docs: "The `_FILE` environment variables demonstrated here are a convention used by some images, including Docker Official Images like mysql and postgres." Examples: `MYSQL_ROOT_PASSWORD_FILE=/run/secrets/db_root_password`.
- It is a convention the application must implement, not a Docker feature.
- Recommended for this service: support `FOO_FILE` only for secrets (no `FOO` fallback in production), fail fast at startup if the file is missing, empty or unreadable, and trim exactly one trailing `\r?\n`.

### 1.4 Windows / Docker Desktop caveats for file-sourced Compose secrets

- **`uid`/`gid`/`mode` are ignored.** Compose reference: these attributes "are only implemented in Docker Compose when the source of the secret is `environment`. When the source is a `file`, Compose uses a bind-mount under the hood which doesn't allow `uid` remapping, and these attributes are silently ignored."
- **Permissions are fixed at 0777 from a Windows host path.** Docker Desktop docs: "Docker Desktop sets permissions on shared volumes to a default value of 0777"; "The default permissions on shared volumes are not configurable." So a non-root app can read the file, but the app must not enforce "secret must be 0400/0600" checks, and no in-container permission boundary exists. Protect the host file with NTFS ACLs and keep it out of git and out of the build context.
- **Line endings / trailing newline.** UNVERIFIED in Docker docs (not documented there): the file is bind-mounted byte-for-byte, so a CRLF or trailing newline written by a Windows editor or `echo` becomes part of the token. Mitigate in code (trim) and write files without a newline (`Set-Content -NoNewline`, or `printf '%s'`). Also add `secrets/** -text` in `.gitattributes` if any encrypted secret file is ever versioned (INFERENCE).
- **`environment:`-sourced secrets vs `read_only: true`.** UNVERIFIED (Docker forum report only, not reproduced, no fix confirmed): startup may fail with "Container rootfs is marked read-only" because Compose writes env-sourced secrets into the container filesystem. Test this on the target Compose version before relying on the combination; file-sourced secrets are bind mounts and should not be affected.
- Swarm on Windows *containers* stores secrets "in clear text to the container's root disk" - not relevant to Linux containers on Docker Desktop.

### 1.5 Recommendation per environment

| Environment | Recommendation |
|---|---|
| Local dev | Compose `secrets:` with `file:` pointing at a gitignored `./secrets/` directory; sandbox/test vendor credentials only; `.env` for non-secret config |
| Compose (single host) | Same mechanism; long syntax; one secret per credential; grant only to the publishing service |
| CI | GitHub Actions secrets -> Compose `environment:` source or `--secret id=...,env=...` for builds. Unit/integration tests should use fakes, not real vendor tokens |
| Production | Orchestrator secret store or external manager, always delivered as files at `/run/secrets/*` so the app code is identical everywhere |

### 1.6 CI (GitHub Actions) facts

- "Secrets use Libsodium sealed boxes, so that they are encrypted before reaching GitHub."
- Log redaction exists but "this redaction is not guaranteed"; "the runner can only redact secrets used within the current job."
- "Never use structured data as a secret" (breaks redaction); register derived sensitive values as secrets.
- "Any user with write access to your repository has read access to all secrets configured in your repository."
- Environment secrets can require reviewer approval; use OIDC for cloud providers instead of long-lived keys; default `GITHUB_TOKEN` to read-only.
- "Pinning an action to a full-length commit SHA is currently the only way to use an action as an immutable release." (See the Trivy incident in section 5.)
- Do not check out untrusted code in `pull_request_target` workflows.
- Fork-PR secret withholding and secret size limit: UNVERIFIED (not on the pages opened).

---

## 2. Mutable credentials (refreshed OAuth tokens)

No Docker source addresses this directly; the pattern below is INFERENCE built on the cited facts.

Facts:
- Compose and Swarm secrets are mounted read-only; Swarm secrets are immutable (rotate by creating a new named secret).
- Docker volumes are "persistent data stores for containers"; the volumes page says nothing about encryption, i.e. Docker does not encrypt volume contents.
- OWASP Crypto Storage: use authenticated encryption (GCM/CCM first preference); "encryption keys should be stored in a separate location from encrypted data"; DEK/KEK envelope scheme with the KEK stored separately; rotate keys on compromise or after a set period.
- RFC 9700: refresh tokens may be rotated by the authorization server; tokens must not be stored or transferred in plaintext.

Pattern:
1. **Bootstrap secrets, read-only, injected**: client IDs/secrets, app secrets, bot tokens, the initial refresh token, and a master key (KEK) for the token store. Delivered as `/run/secrets/*`.
2. **Token store, writable, encrypted at rest**: current access/refresh tokens and expiry, encrypted with AEAD under a DEK wrapped by the injected KEK, on a named volume mounted at one path (for example `/data`). Key and ciphertext are thereby in different places, as OWASP requires.
3. Root filesystem stays `read_only: true`; the volume and a `tmpfs` for `/tmp` are the only writable paths.

Implications:
- The injected refresh token is only a seed. After first rotation the volume holds the truth; re-injecting the old seed must not overwrite a newer stored token (store an issued-at/version and prefer the newer).
- The volume becomes backup-critical and secret-bearing. Losing it means re-running the interactive OAuth consent flow.
- Single writer: if the vendor rotates refresh tokens, two replicas refreshing concurrently can invalidate each other. Run one instance or add a lock/leader.
- Write atomically (temp file + rename in the same directory) so a crash cannot truncate the store.
- Use a named volume, not a Windows bind mount (0777 permissions, see 1.4). Named-volume ownership must match the numeric runtime uid; an empty volume inherits the image directory's contents and ownership on first mount ("files or directories are propagated (copied) into the volume by default"), so create and `chown` the directory in the image.
- KEK rotation = re-wrap the DEK; no vendor interaction required.
- Production alternative: write refreshed tokens back to the external secret manager through its API, which removes the volume but adds a write-capable manager credential.

---

## 3. Image build

### 3.1 Dockerfile practice

| Practice | Source statement / detail |
|---|---|
| Multi-stage | "You can selectively copy artifacts from one stage to another, leaving behind everything you don't want in the final image." BuildKit "only builds the stages that the target stage depends on." |
| Base image | Prefer Docker Official Images / Verified Publisher / Docker Hardened Images. Best-practices page still says "Docker recommends the Alpine image as it is tightly controlled and small in size (under 6 MB)." |
| Pin by digest | "By pinning your images to a digest, you're guaranteed to always use the same image version, even if a publisher replaces the tag." Form: `FROM name:tag@sha256:...`. Cost: you must bump digests yourself (Dependabot/Renovate - UNVERIFIED tooling detail) |
| Rebuild often | "rebuild your images regularly with updated dependencies"; use `--pull` |
| `.dockerignore` | "create a `.dockerignore` file in the root of your build context." Must exclude `.env*`, `secrets/`, `.git`, token stores, local volumes |
| Cache mounts | `RUN --mount=type=cache,target=...`; contents are not part of the image. Bind mounts for sources likewise "are not persisted in the final image, or in the build cache" |
| Layer order | Dependency manifests and install before source copy; frequently changing steps last |
| `COPY` not `ADD` | `ADD` only for remote/tar features |
| `USER` | "Consider an explicit UID/GID". Use numeric `USER 10001:10001` (INFERENCE: numeric works without `/etc/passwd` and satisfies Kubernetes `runAsNonRoot`). "Avoid installing or using `sudo`" |
| Exec-form `ENTRYPOINT` | Shell form "starts your `ENTRYPOINT` as a subcommand of `/bin/sh -c`, which does not pass signals"; the executable "will not be the container's `PID 1`, and will not receive Unix signals." Distroless requires vector form anyway |
| No secrets in `ARG`/`ENV` | See 1.1 |
| Reproducibility | `SOURCE_DATE_EPOCH` makes "timestamps in the image index, config, and file metadata reflect the specified Unix time" (e.g. `git log -1 --pretty=%ct`). Also needs digest-pinned bases and lockfile installs. `rewrite-timestamp` exporter option: UNVERIFIED (not on the page opened). Bit-for-bit reproducibility is not guaranteed by these steps alone |
| Attestations | `--sbom=true`, `--provenance=mode=max`. "Provenance attestations with the `mode=min` level are added to images by default." Needs the containerd image store with the `docker` driver; Docker Desktop "uses containerd as its image store by default." Warning: "`mode=max` exposes the values of build arguments." |
| `docker init` | Docker Desktop CLI plugin; generates `.dockerignore`, `Dockerfile`, `compose.yaml`, `README.Docker.md`; templates: ASP.NET Core, Go, Java, Node, PHP with Apache, Python, Rust, Other. Prompts before overwriting, and overwritten files cannot be recovered. A reasonable starting scaffold, not a hardened result |

### 3.2 Base image choice

| Option | Facts | Assessment |
|---|---|---|
| Docker Hardened Images (DHI) | "DHI's core features are free to use, share, and build on under Apache 2.0." Tiers: Community (free), Select, Enterprise; the 7-day critical/high CVE remediation SLA, FIPS/STIG variants and customisation are paid tiers. Pull from `dhi.io/<image>:<tag>` after `docker login dhi.io`. Runtime images have no shell or package manager and "run as non-root users by default"; `-dev` tags for build stages; `-static` for compiled binaries. Signed SBOM, SLSA Build Level 3 provenance, VEX on every image | First choice if a DHI exists for the chosen runtime. Requires registry login in CI. No CVE SLA on the free tier |
| Distroless (`gcr.io/distroless/*`) | Now Debian 13 based; `static`, `base`, `base-nossl`, `cc`, Java 17/21/25, Node 22/24/26, `python3`; tags `latest`, `nonroot`, `debug`, `debug-nonroot`; no shell or package manager; cosign keyless signatures | Equivalent fallback. `nonroot` uid 65532: UNVERIFIED (not in the fetched excerpt) |
| `-slim` official images | Debian with shell and package manager, glibc | Use for the build stage, or runtime if a native dependency blocks distroless |
| Alpine | Small; still Docker's stated general recommendation; musl libc | Fine for static binaries; musl incompatibilities with some native wheels/modules: UNVERIFIED (not sourced here) |

Debugging shell-less images: `docker debug` attaches "a temporary, tool-rich debug container to the running instance."

### 3.3 Runtime hardening - where each setting lives

| Control | Dockerfile | Compose key | Note |
|---|---|---|---|
| Non-root user | `USER 10001:10001` | `user: "10001:10001"` (override) | OWASP rule #2. Listen on a port above 1024 |
| Read-only root FS | - | `read_only: true` | OWASP rule #8. Add `tmpfs: [/tmp]` and the data volume |
| Drop capabilities | - | `cap_drop: [ALL]` | Docker: "remove all capabilities except those explicitly required"; OWASP rule #3 |
| No privilege escalation | - | `security_opt: ["no-new-privileges:true"]` | OWASP rule #4 |
| Init / zombie reaping | - | `init: true` | Runs `docker-init` (tini) as PID 1, "forwards signals and reaps processes" |
| Graceful shutdown | `STOPSIGNAL` (optional), exec-form entrypoint | `stop_signal`, `stop_grace_period` | Default SIGTERM, then SIGKILL after 10 s |
| Health check | `HEALTHCHECK` | `healthcheck:` (overrides image) | See section 4 |
| Resource limits | - | `deploy.resources.limits` / `mem_limit`, `pids_limit` | OWASP rule #7. Exact Compose keys: UNVERIFIED (not opened) |
| Network exposure | `EXPOSE` (documentation only) | no `ports:`; `expose:`; `networks:` | See section 4 |
| Secrets | - | top-level `secrets:` + service `secrets:` | Section 1 |
| Never | - | `privileged: true`, mounting `/var/run/docker.sock` | OWASP rules #1, #3 |

Other OWASP Docker rules worth carrying into SECURITY.md: keep host and engine patched (#0), keep the default seccomp/AppArmor profiles (#6), scan images in CI (#9), rootless mode (#11), supply-chain controls - provenance, SBOM, signing (#13).

### 3.4 HEALTHCHECK without curl

- Dockerfile reference defaults: `--interval=30s --timeout=30s --start-period=0s --start-interval=5s --retries=3`; exit 0 healthy, 1 unhealthy; "There can only be one HEALTHCHECK instruction in a Dockerfile."
- Docker's own example uses `curl`, which distroless/DHI runtime images do not have, and shell-form `CMD` needs `/bin/sh`.
- INFERENCE: use exec form and the application's own binary/runtime, e.g. `HEALTHCHECK CMD ["/app/service", "healthcheck"]` (a subcommand that performs a loopback HTTP GET and sets the exit code), or the language runtime (`["node", "healthcheck.js"]`, `["python", "-m", "app.healthcheck"]`). In Compose use `test: ["CMD", ...]`, not `CMD-SHELL`.
- Do not install curl/wget just for this; it re-adds an exfiltration tool to a container holding vendor credentials.

---

## 4. Liveness vs readiness; how callers reach and wait for the service

- Docker has a single health state per container: `starting` -> `healthy` / `unhealthy`. No liveness/readiness split.
- Restart policies act on exit codes (`no`, `on-failure[:max-retries]`, `always`, `unless-stopped`); the restart-policy page does not mention health. Plain Docker/Compose not restarting an `unhealthy` container: UNVERIFIED as an explicit doc statement, consistent with the page. Swarm replacing unhealthy tasks: UNVERIFIED.
- Consequence (INFERENCE): in Compose the health check is effectively a **readiness** gate consumed by `depends_on`. Liveness = the process exits non-zero on unrecoverable faults and `restart: unless-stopped` restarts it.
- Design: expose two endpoints so Kubernetes can use both later - `/livez` (process responsive, no dependencies) and `/readyz` (secrets loaded, token store readable, ready to accept work). Point the Docker `HEALTHCHECK` at `/readyz`. Do not call vendor APIs from either: an outage or rate limit at Telegram/Meta/LinkedIn would mark the service unhealthy and block callers.
- `start_period`: failures during it do not count; `start_interval` (Compose 2.20.2+) allows fast polling while starting, so `service_healthy` resolves quickly without a short steady-state interval.

Reaching the service:
- "By default, Compose sets up a single network for your app" named `<project-name>_default`; "containers can reach each other using the service name directly."
- "Networked service-to-service communication uses the `CONTAINER_PORT`. The host port is only used when accessing the service from outside the network." Callers use `http://<service-name>:<container-port>`.
- Do not publish `ports:` for this service. Short syntax without a host IP "binds to all network interfaces (`0.0.0.0`)". If host access is needed for debugging, bind `127.0.0.1:PORT:PORT` (OWASP rule #5a).
- `expose:` ports "must be accessible to linked services and should not be published to the host machine" - documentation value; same-network containers can reach any listening port regardless (UNVERIFIED as a doc statement).
- Put the publisher and its callers on a dedicated user-defined network; `internal: true` creates a network with "no default gateway for external connectivity". The publisher itself needs outbound internet for vendor APIs, so attach it to an internal network for callers plus a second network for egress (INFERENCE).
- Callers from another Compose project join via a pre-created network marked `external: true`.
- No authentication is implied by network placement: any container on the network can call the service. Service-level auth for callers is a separate decision.

Waiting:
- "On startup, Compose does not wait until a container is 'ready', only until it's running."
- `depends_on: publisher: { condition: service_healthy, restart: true }` - `service_healthy` waits for the health check; `restart: true` restarts the dependent after Compose updates the dependency; `required: false` downgrades to a warning.
- `depends_on` only governs startup order within one `docker compose up`. Callers still need retry with backoff, since the service can restart later.

---

## 5. Verifying no secrets are baked into the image

| Check | Covers | Status (2026-10-09) |
|---|---|---|
| `docker image history --no-trunc <img>` | Layer-creating commands (`.CreatedBy`), including `ARG`/`ENV` values used in them | Built in |
| `docker image inspect <img>` | `Config.Env`, labels, entrypoint/cmd (INFERENCE from the inspect reference; Env field not shown on the page) | Built in |
| `docker buildx imagetools inspect <ref> --format '{{ json .Provenance }}'` | Provenance attestation; `mode=max` embeds build args and the Dockerfile | Command form UNVERIFIED (not on the attestation pages opened) |
| Trivy `trivy image --scanners secret <img>` | "scan every plaintext file" in the image against built-in rules (AWS, GCP, GitHub, GitLab, Slack...) plus custom rules. Whether it also scans image config/env: UNVERIFIED | Active; latest v0.75.0 (2026-10-01). **Supply-chain incident March 2026 (CVE-2026-33634)**: malicious binary v0.69.4, Docker Hub images 0.69.5/0.69.6, 76 of 77 `aquasecurity/trivy-action` tags and `setup-trivy` tags force-pushed to credential-stealing code. Use a current release, verify it, and pin the action by full commit SHA. Add custom rules for Telegram/Meta/LinkedIn token formats (built-in coverage for those: UNVERIFIED) |
| dive (`wagoodman/dive`) | Interactive per-layer file diff; `CI=true` gives pass/fail on size efficiency | Maintained but slow-moving; latest v0.13.1 (2025-03-29); self-described "beta quality". A manual inspection aid, not a secret scanner |
| Docker Scout (`docker scout cves`, `quickview`) | SBOM + CVE matching; sources `image://`, `local://`, `registry://`, `oci-dir://`, `archive://`, `fs://`, `sbom://`; `--exit-code`, `--only-severity`, `--format sarif` | Active (CLI v1.18.3 locally). **Not a secret scanner** - docs describe vulnerability analysis only |
| Gitleaks / TruffleHog / ggshield | Named by OWASP for secret detection (repo and pre-commit; image support varies - UNVERIFIED) | Not checked |
| `docker save <img> -o img.tar`, then scan/grep the extracted layers | Files deleted in a later layer are still present in the earlier layer | Manual |

Suggested CI gate (INFERENCE): build -> history/inspect grep for known secret variable names -> Trivy secret + vuln scan pinned by SHA -> Scout CVE scan -> a canary test that builds with a fake token present in the context and asserts it is absent from the saved image tar.

Prevention beats detection: nothing secret in the build context (`.dockerignore`), no secret `ARG`/`ENV`, `--mount=type=secret` for any build-time credential, and no vendor tokens at build time at all.

---

## UNVERIFIED items (consolidated)

1. `docker inspect` / `/proc/<pid>/environ` exposing container env - well known, not stated on the pages opened.
2. Trailing newline/CRLF being preserved in file-sourced secrets - not documented by Docker; follows from the bind-mount implementation.
3. `environment:`-sourced Compose secrets failing with `read_only: true` - one forum report, not reproduced (local daemon was not running).
4. Distroless `nonroot` uid 65532.
5. Alpine/musl compatibility problems with native dependencies.
6. BuildKit `rewrite-timestamp` option; full bit-for-bit reproducibility.
7. Plain Docker/Compose never restarting `unhealthy` containers; Swarm doing so.
8. `expose:` being purely informational on a shared network.
9. Compose resource-limit key names.
10. `docker buildx imagetools inspect` provenance format string.
11. Trivy scanning image config/env for secrets; built-in rules for Telegram/Meta/LinkedIn tokens.
12. GitHub Actions: secrets withheld from fork PRs; secret size limit.
13. Vault / AWS / GCP / Azure secret-manager specifics - no vendor docs were opened; only OWASP's and Kubernetes' descriptions of the pattern.
14. Docker Desktop-native secret tooling (e.g. a Desktop secrets engine) - not researched.
15. Dependabot/Renovate support for digest bumps.

---

## Sources consulted

Docker docs
- https://docs.docker.com/compose/how-tos/use-secrets/
- https://docs.docker.com/reference/compose-file/secrets/
- https://docs.docker.com/reference/compose-file/services/
- https://docs.docker.com/compose/how-tos/environment-variables/best-practices/
- https://docs.docker.com/compose/how-tos/startup-order/
- https://docs.docker.com/compose/how-tos/networking/
- https://docs.docker.com/build/building/secrets/
- https://docs.docker.com/build/building/best-practices/
- https://docs.docker.com/build/building/multi-stage/
- https://docs.docker.com/build/cache/optimize/
- https://docs.docker.com/build/ci/github-actions/reproducible-builds/
- https://docs.docker.com/build/metadata/attestations/
- https://docs.docker.com/build/metadata/attestations/slsa-provenance/
- https://docs.docker.com/reference/dockerfile/
- https://docs.docker.com/reference/cli/docker/init/
- https://docs.docker.com/reference/cli/docker/container/run/
- https://docs.docker.com/reference/cli/docker/inspect/
- https://docs.docker.com/reference/cli/docker/image/history/
- https://docs.docker.com/reference/cli/docker/scout/cves/
- https://docs.docker.com/scout/
- https://docs.docker.com/engine/swarm/secrets/
- https://docs.docker.com/engine/security/
- https://docs.docker.com/engine/storage/volumes/
- https://docs.docker.com/engine/containers/start-containers-automatically/
- https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/topics/
- https://docs.docker.com/desktop/features/containerd/
- https://docs.docker.com/dhi/
- https://docs.docker.com/dhi/how-to/use/
- https://docs.docker.com/dhi/troubleshoot/
- https://forums.docker.com/t/feature-request-ability-to-use-secrets-with-read-only-rootfs/145002 (forum, low confidence)

OWASP / IETF
- https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html
- https://datatracker.ietf.org/doc/html/rfc9700

Kubernetes
- https://kubernetes.io/docs/concepts/configuration/secret/
- https://kubernetes.io/docs/concepts/security/secrets-good-practices/

GitHub
- https://docs.github.com/en/actions/concepts/security/secrets
- https://docs.github.com/en/actions/reference/security/secure-use

Tools
- https://github.com/GoogleContainerTools/distroless
- https://github.com/wagoodman/dive
- https://github.com/wagoodman/dive/releases
- https://github.com/aquasecurity/trivy/releases
- https://trivy.dev/latest/docs/scanner/secret/ (page flagged itself as not the latest docs version)
- https://www.aquasec.com/blog/trivy-supply-chain-attack-what-you-need-to-know

Search results only (snippets read, pages not opened): two web searches, on DHI licensing and on the Trivy incident; the DHI licensing facts above are taken from docs.docker.com/dhi/, and the Trivy facts from the Aqua blog and the releases page.
