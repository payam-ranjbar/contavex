# LinkedIn integration research

Researched: 2026-10-09 (initial pass — must be re-verified immediately before implementation)

Current `LinkedIn-Version`: **202609** (September 2026; sunset 2027-09-15). Oldest still-active version on the research date: 202510 (sunset 2026-10-15).

Conventions in this document:

- Every fact comes from a page listed under "Official documentation references" unless tagged otherwise.
- **UNVERIFIED** = not confirmed in a LinkedIn primary source during this pass (either undocumented, or only found in third-party sources). Do not design around these without re-checking.
- LinkedIn is the third integration (after Telegram and Instagram). This is architecture-level research, not an implementation spec.

---

## Authentication

**Model: OAuth 2.0, 3-legged authorization code flow, acting on behalf of a LinkedIn member.** There is no bot token and no app-only credential for posting.

| Item | Value |
| --- | --- |
| Authorization endpoint | `GET https://www.linkedin.com/oauth/v2/authorization` |
| Token endpoint | `POST https://www.linkedin.com/oauth/v2/accessToken` (`Content-Type: application/x-www-form-urlencoded`) |
| Token introspection | `POST https://www.linkedin.com/oauth/v2/introspectToken` (`client_id`, `client_secret`, `token`) |
| OIDC discovery | `https://www.linkedin.com/oauth/.well-known/openid-configuration` |
| API call auth header | `Authorization: Bearer {access_token}` |

Authorization request parameters: `response_type=code`, `client_id`, `redirect_uri`, `scope` (URL-encoded, space-delimited), `state` (documented as optional; use it for CSRF protection).

Token exchange parameters: `grant_type=authorization_code`, `code`, `client_id`, `client_secret`, `redirect_uri`.

Facts that shape the design:

- **An interactive browser consent by a real member is unavoidable** for the first token. The member logs in on linkedin.com and approves all requested scopes as a block (no per-scope selection).
- Redirect URIs must be pre-registered on the app, absolute, HTTPS, no `#` fragment; query parameters are ignored. A containerised service therefore needs either a reachable HTTPS callback or an out-of-band setup step that captures the `code` (the Developer Portal also has a manual Token Generator tool for obtaining tokens by hand).
- The authorization code lives **30 minutes** and is single-purpose.
- **Client credentials (2-legged) flow cannot be used for posting.** The docs state "2-legged OAuth authentication is not available for Marketing APIs", applications cannot use it by default, and its tokens last 30 minutes. All Marketing/Community Management permissions are "Member Permission (3-legged)".
- Requesting a different scope set than previously granted **invalidates all previous access tokens** for that member/app. Changing the app's scopes requires members to re-authenticate.
- Multiple valid access tokens can coexist if the same scope is requested.
- PKCE: the token endpoint error text mentions a "code verifier", but the authorization-code-flow page does not document PKCE parameters. **UNVERIFIED** whether PKCE is available for this (confidential-client) flow; not needed for a server-side service holding a client secret.

Required headers on API calls:

| Header | Applies to | Value |
| --- | --- | --- |
| `Authorization` | all | `Bearer {token}` |
| `Linkedin-Version` | all versioned `/rest/*` calls | `YYYYMM`, e.g. `202609` (docs spell it `Linkedin-Version` and `LinkedIn-Version`; HTTP header names are case-insensitive) |
| `X-Restli-Protocol-Version` | all | `2.0.0` |
| `Content-Type` | JSON bodies | `application/json` |
| `X-RestLi-Method` | some operations | `PARTIAL_UPDATE` (edit), `DELETE` (shown in delete sample), `BATCH_GET`, `FINDER` |

---

## Account requirements

Two distinct publishing targets exist, with different authors, products and approval paths.

### A. Personal profile (author = member)

- A LinkedIn member account that completes the consent flow.
- A LinkedIn developer application (Developer Portal). Creating an app requires associating it with a LinkedIn Page.
- Products added to the app (both listed as **open / self-serve** permissions, no review):
  - **Share on LinkedIn** → scope `w_member_social`.
  - **Sign In with LinkedIn using OpenID Connect** → scopes `openid`, `profile`, `email` (needed to learn the member's ID).
- Author URN: `urn:li:person:{id}`.

### B. Company page (author = organization)

- A LinkedIn Page, and an authenticating member who holds a suitable role on that Page (see Permissions).
- A developer app approved for the **Community Management API** (vetted product, Development tier then Standard tier — see Production requirements).
- Author URN: `urn:li:organization:{id}` (showcase/brand pages use `urn:li:organizationBrand:{id}` in several organization APIs).
- LinkedIn's restricted-use page forbids "headless or fake accounts": do not create fake profiles or use one individual's profile to manage multiple customers' accounts. The authenticating member must be a real person with a real role on the Page.

### Finding the author URN

- **Person:** `GET https://api.linkedin.com/v2/userinfo` with the member's token (requires `openid` + `profile`); the `sub` field is the member identifier. The Share on LinkedIn guide points to this for retrieving the Person URN (`urn:li:person:{sub}`). The Post schema page instead points at the "Retrieve Current Member's Profile" API (`id` field), which needs profile permissions (`r_basicprofile` under Community Management API). The same value is also available in the ID token `sub` claim (JWT, RS256, JWKS at `https://www.linkedin.com/oauth/openid/jwks`).
- **Organization:** `GET https://api.linkedin.com/rest/organizationAcls?q=roleAssignee` (optionally `&role=ADMINISTRATOR&state=APPROVED`) returns the organizations the authenticated member has roles on. Requires `rw_organization_admin` (or `r_organization_admin`).

---

## Permissions

| Scope | Purpose | Granted by | Notes |
| --- | --- | --- | --- |
| `w_member_social` | Post, comment, like on behalf of the authenticated member | Share on LinkedIn (self-serve); also Community Management API, Advertising API | Write-only: a token with only this scope **cannot GET `/rest/images`** on the versioned gateway |
| `openid`, `profile`, `email` | Identify the member (OIDC) | Sign In with LinkedIn using OpenID Connect (self-serve) | |
| `w_organization_social` | Post, comment, like on behalf of an organization | Community Management API, Advertising API | Member must have Page role `ADMINISTRATOR`, `DIRECT_SPONSORED_CONTENT_POSTER` or `CONTENT_ADMIN` |
| `r_organization_social` | Read organization posts, comments, likes | Community Management API, Advertising API | Same three Page roles |
| `rw_organization_admin` | Manage Pages, read reporting data, `organizationAcls`, share statistics | Community Management API, Advertising API | Page role `ADMINISTRATOR` only |
| `r_basicprofile` | Member name, photo, headline, public profile URL | Community Management API, Advertising API | |
| `r_member_postAnalytics` | Analytics for the member's own posts | Community Management API | API versions 202506+ |
| `r_member_profileAnalytics` | Member profile analytics | Community Management API | API versions 202504+ |
| `r_member_social` | Read a member's posts, comments, likes | — | **Closed**: "We're not accepting access requests at this time" |
| `w_member_social_feed`, `w_organization_social_feed`, `r_organization_social_feed`, `r_organization_followers`, `r_1st_connections_size` | Comments/reactions, follower data | Community Management API | Not needed for publishing |

Page role names: the Posts API page lists `ADMINISTRATOR`, `DIRECT_SPONSORED_CONTENT_POSTER`, `CONTENT_ADMIN`; the Organization Access Control page names the role `CONTENT_ADMINISTRATOR`. Treat both spellings as possible when reading `organizationAcls`. Page roles cannot be granted or changed through the API — only in the LinkedIn UI.

Media asset permission checks (Images, Videos, Documents): for organization-owned assets the caller needs ADMIN or DSC permission on the Page; for member-owned assets the caller must be the owner.

---

## Publishing workflow

### API surface and status

| API | Path | Status |
| --- | --- | --- |
| **Posts API** | `https://api.linkedin.com/rest/posts` | Current. "The Posts API replaces the ugcPosts API." Versioned (`Linkedin-Version` required). |
| UGC Posts API | `https://api.linkedin.com/v2/ugcPosts` | Legacy. Content APIs replaced `ugcPosts`/`assets` in the Marketing platform (migration sunset date 2023-06-30); unversioned Marketing APIs listed as deprecated (sunset 2024-12-16). **However**, the consumer "Share on LinkedIn" guide (page last updated 2023-12) still documents `POST /v2/ugcPosts` + `POST /v2/assets?action=registerUpload` as the way to share with `w_member_social`. |
| Shares API | `/v2/shares` | Legacy; replaced by Posts API (see migration guide field mapping). |
| Assets API | `/rest/assets`, `/v2/assets` | Legacy; "won't have any feature additions and will be deprecated in the future". Replaced by Images API and Videos API. |

**UNVERIFIED:** whether a self-serve "Share on LinkedIn" app (no Community Management API) can call the versioned `/rest/posts`, `/rest/images`, `/rest/videos` with `w_member_social`. The Posts API page lists `w_member_social` as a valid permission with no product restriction, but the only official self-serve guide uses the legacy `/v2/ugcPosts` endpoint. This must be tested with a real app before implementation, because it decides which request schema the member-posting path uses.

### Create a post (Posts API)

```http
POST https://api.linkedin.com/rest/posts
Authorization: Bearer {token}
Linkedin-Version: 202609
X-Restli-Protocol-Version: 2.0.0
Content-Type: application/json

{
  "author": "urn:li:organization:5515715",
  "commentary": "Sample text Post",
  "visibility": "PUBLIC",
  "distribution": {
    "feedDistribution": "MAIN_FEED",
    "targetEntities": [],
    "thirdPartyDistributionChannels": []
  },
  "lifecycleState": "PUBLISHED",
  "isReshareDisabledByAuthor": false
}
```

- Success: **`201`** with an empty body; the post identifier is in the **`x-restli-id` response header**, e.g. `urn:li:share:6844785523593134080` or `urn:li:ugcPost:68447855235931240`. Either URN type can come back; treat the ID as an opaque string.
- Required fields: `author` (create-only), `commentary`, `visibility`, `distribution` (create-only), `lifecycleState`.
- `lifecycleState`: only `PUBLISHED` is accepted on create. Responses can also show `DRAFT`, `PUBLISH_REQUESTED` (submitted, published asynchronously once processing completes), `PUBLISH_FAILED` (an edit is required to retry).
- `lifecycleStateInfo.contentStatus` / `reviewStatus` can be `PENDING`, `PROCESSING`, `FAILED` while content is processed or reviewed.
- `visibility` values: `PUBLIC`, `CONNECTIONS`, `LOGGED_IN`, `CONTAINER`.
- `feedDistribution`: `MAIN_FEED` or `NONE` (`NONE` + `PUBLIC` + organization author = dark post for ads).
- There is no scheduling field; scheduling must be done by the caller.

### Content types

| Type | `content` shape | Notes |
| --- | --- | --- |
| Text only | omit `content` | |
| Single image | `content.media: { id: "urn:li:image:…", altText }` | Upload via Images API first |
| Video | `content.media: { id: "urn:li:video:…", title }` | Upload via Videos API first |
| Document | `content.media: { id: "urn:li:document:…", title }` | `title` is required for documents |
| Multi-image | `content.multiImage.images: [{ id, altText }, …]` | 2–20 images, organic only |
| Article / link | `content.article: { source, title, description, thumbnail }` | **No URL scraping**: the API does not fetch link previews. The caller must supply title/description and upload a thumbnail image (Images API) itself |
| Poll | `content.poll` | Organic only (not researched in depth) |
| Reshare | top-level `reshareContext.parent` | |
| Carousel | `content.carousel` | Sponsored only — not available for organic posts |

### Text format ("little" text)

`commentary` is a single string in LinkedIn's `little` format:

- Mention: `@[Display Name](urn:li:organization:2414183)` or `@[Name](urn:li:person:…)`. The bracket text must match the entity name (case-sensitive; organizations must match the full name, members may match any one name part) or it renders as plain text.
- Hashtag: `#coding` in input. On read it comes back as the template `{hashtag|\#|coding}`. The template form can also be sent.
- **Reserved characters must be backslash-escaped even when not used as markup:** `|`, `{`, `}`, `@`, `[`, `]`, `(`, `)`, `<`, `>`, `#`, `\`, `*`, `_`, `~`. (An intentionally written hashtag or mention is the unescaped exception.) Unescaped parentheses, underscores, asterisks etc. in ordinary prose can corrupt or truncate the post, so the adapter needs an escaping step for plain-text input.
- Line breaks are `\n` inside the JSON string.

### Media upload (binary upload to LinkedIn — not URL ingestion)

LinkedIn has **no "fetch from public URL"** path for images, videos or documents. The service must hold the bytes and upload them.

**Images API**

1. `POST https://api.linkedin.com/rest/images?action=initializeUpload` with `{ "initializeUploadRequest": { "owner": "urn:li:organization:…" } }` → `200` with `value.uploadUrl`, `value.uploadUrlExpiresAt`, `value.image` (`urn:li:image:…`).
2. `PUT` the bytes to `uploadUrl` (curl `--upload-file`). The image upload call **requires** the `Authorization: Bearer` header. Response `201`.
3. Optional status check: `GET https://api.linkedin.com/rest/images/{urn}` → `status` ∈ `WAITING_UPLOAD`, `PROCESSING`, `AVAILABLE`, `PROCESSING_FAILED`. `SYNCHRONOUS_UPLOAD` is not supported by the Images API, so processing is asynchronous. Member tokens holding only `w_member_social` cannot perform this GET on the versioned gateway.

**Videos API** (always multipart)

1. `POST https://api.linkedin.com/rest/videos?action=initializeUpload` with `owner`, `fileSizeBytes` (required), `uploadCaptions`, `uploadThumbnail` → `value.video` (`urn:li:video:…`), `value.uploadToken`, `value.uploadUrlsExpireAt`, `value.uploadInstructions[]` (each `{ firstByte, lastByte, uploadUrl }`).
2. Split the file into **4 MB parts** (byte ranges 0–4194303, 4194304–8388607, …) and `PUT` each part to its `uploadUrl` with `Content-Type: application/octet-stream`. Per the legacy Assets page, the video upload call must **not** carry an OAuth `Authorization` header (the opposite of images). Each response returns an **`ETag`** header — keep them in order.
3. Optional: upload captions (SRT, multipart form, English only, one file) to `captionsUploadUrl`; upload thumbnail to `thumbnailUploadUrl` (header `media-type-family: STILLIMAGE`).
4. `POST https://api.linkedin.com/rest/videos?action=finalizeUpload` with `{ "finalizeUploadRequest": { "video", "uploadToken", "uploadedPartIds": [etag1, etag2, …] } }` → `200`.
5. Poll `GET https://api.linkedin.com/rest/videos/{encoded urn}` until `status` is `AVAILABLE` (or `PROCESSING_FAILED`, with `processingFailureReason`). No webhook for media processing was found.

Upload URLs typically expire 30 days after initialization; uploading to an expired URL returns `401` (schema note) / `400 EXPIRED_UPLOAD_URL` (error table).

**Documents API**

1. `POST https://api.linkedin.com/rest/documents?action=initializeUpload` with `owner` → `uploadUrl`, `uploadUrlExpiresAt`, `document` (`urn:li:document:…`).
2. `PUT` the file to `uploadUrl` with the `Authorization: Bearer` header → `201`.
3. `GET https://api.linkedin.com/rest/documents/{urn}` for `status` (same four values).

**Ordering rule:** the legacy Assets page warns that if a post is created before the image upload has finished processing and processing then fails, the post is created but not visible to members. For the Images/Videos/Documents APIs, wait for `AVAILABLE` before creating the post where the token allows the GET; otherwise expect `PUBLISH_REQUESTED` / processing states on the post.

Media assets are reusable across posts once uploaded.

### Edit a post

```http
POST https://api.linkedin.com/rest/posts/{url-encoded post URN}
X-RestLi-Method: PARTIAL_UPDATE

{ "patch": { "$set": { "commentary": "Update to the post" } } }
```

Success `204`. Only these fields are updatable: `commentary`, `contentCallToActionLabel`, `contentLandingPage`, `lifecycleState`, `adContext` (`dscName`, `dscStatus`). **Media and article content cannot be changed after creation.**

### Delete a post

`DELETE https://api.linkedin.com/rest/posts/{url-encoded post URN}` (sample also sends `X-RestLi-Method: DELETE`) → `204`. "Post deletions are idempotent" — deleting an already-deleted post also returns `204`. Batch delete is not supported.

### Read a post

`GET https://api.linkedin.com/rest/posts/{url-encoded URN}` with optional `viewContext=AUTHOR` (default `READER`; `AUTHOR` shows the latest, possibly unpublished/processing version). URNs in URLs must be URL-encoded (`urn%3Ali%3AugcPost%3A12345`). Reading organization posts needs `r_organization_social`; reading member posts needs the closed `r_member_social`.

Public URL of a published post: `https://www.linkedin.com/feed/update/{post URN}/` (documented for `urn:li:ugcPost:<id>`).

---

## Supported media

| Media | Formats | Limits |
| --- | --- | --- |
| Image | JPG, GIF, PNG | Fewer than 36,152,320 pixels; GIF up to 250 frames. `altText` max 4,086 characters (recommended < 120). No byte-size limit stated on the Images API page (**UNVERIFIED**). |
| Multi-image | same as image | Minimum 2, maximum 20 images per post; organic only |
| Video | MP4 | Length 3 seconds – 30 minutes; file size 75 KB – 500 MB (general specification). The schema separately says "Maximum allowed Videos size is 5GB" for `fileSizeBytes` — the two statements conflict; assume 500 MB until tested. Captions: one SRT file, English only. |
| Document | PPT, PPTX, DOC, DOCX, PDF | Max 100 MB and 300 pages |
| Article thumbnail | image (via Images API) | Must be uploaded by the caller; no scraping |

---

## Limits

| Item | Limit | Source status |
| --- | --- | --- |
| `commentary` length | ~3,000 characters | **UNVERIFIED** in LinkedIn docs. The Posts API pages give no number; the documented failure is `400 FIELD_LENGTH_TOO_LONG`. Third-party sources state 3,000 and one reports silent truncation instead of an error. Enforce 3,000 client-side and verify. |
| Images per multi-image post | 2–20 | Verified |
| Image alt text | 4,086 characters | Verified |
| Authorization code lifetime | 30 minutes | Verified |
| Finder page size (`/rest/posts?q=author`) | default 10, max 100 | Verified |
| Targeted organic post audience | must exceed 300 members | Verified |
| Token length | ~500 characters today; plan storage for at least 1,000 (access and refresh tokens) | Verified |
| Video part size | 4 MB (4,194,304-byte ranges) | Verified |
| Upload URL validity | typically 30 days | Verified (videos) |

---

## Rate limits

- Defined as a maximum number of calls per **24-hour period, resetting at midnight UTC**.
- Two limits apply simultaneously: **Application** (all calls by the app per day) and **Member** (calls by one member's token through the app per day).
- Limits vary per endpoint. "Standard rate limits are not published in documentation." They are visible only in the Developer Portal → app → **Analytics** tab, and only for endpoints called at least once that UTC day.
- Throttled requests receive **`429`** (message "Resource level throttle limit for calls to this resource is reached."). LinkedIn may also return `429` occasionally for infrastructure protection; service "will return to normal automatically".
- No `Retry-After` header or rate-limit response headers are documented (**UNVERIFIED** whether any are sent). A daily-quota 429 will not clear until 00:00 UTC, so retry policy must distinguish short backoff from "defer until next UTC day".
- Developer admins get an email when the app exceeds 75% of an application-level quota (delayed 1–2 hours; not sent for member-level breaches).

Published numbers that do exist:

| Context | Application | Member |
| --- | --- | --- |
| Share on LinkedIn (consumer guide, legacy endpoints) | 100,000 requests/day | 150 requests/day |
| Community Management API — Development tier (all APIs) | 500 calls / 24 h | 100 calls / 24 h |
| Community Management API — Development tier, `BATCH_GET` | not allowed | not allowed |
| Community Management API — Standard tier | "No restrictions" (endpoint quotas still shown in the portal) | |

A single media post costs several API calls (initialize, finalize, status polls, create). Whether the binary `PUT`s to `www.linkedin.com/dms-uploads/...` count against quota is **UNVERIFIED**.

---

## Errors

### Response shape

Basic shape (all APIs):

```json
{ "message": "Empty oauth2_access_token", "serviceErrorCode": 401, "status": 401 }
```

- `message` — description; `serviceErrorCode` — subcode; `status` — HTTP status.
- Some endpoints add `code` (stable string such as `FIELD_LENGTH_TOO_LONG`), `errorDetailType` and `errorDetails.inputErrors[]`. LinkedIn states this richer schema is rolled out for ad account/campaign endpoints only; the Posts API page nonetheless lists string codes. Parse `code` when present, fall back to `status` + `message`.
- Batch responses carry per-entity `statuses` and `errors` maps.
- For support tickets LinkedIn asks for response headers `x-li-uuid`, `x-li-fabric`, `x-li-request-id` — log them.

Version errors:

```json
{ "status": 400, "code": "VERSION_MISSING", "message": "A version must be present. Please specify a version by adding the Linkedin-Version header." }
{ "status": 426, "code": "NONEXISTENT_VERSION", "message": "Requested version yyyymmdd is not active" }
```

OAuth endpoint errors use `error` / `error_description` (e.g. `invalid_request` "The provided authorization grant or refresh token is invalid, expired or revoked").

### Retry classification

Classification is this research's recommendation based on the documented meanings; LinkedIn only explicitly says "retry" for 409, 429, 500, 503 and 504.

| Status | Code / meaning | Retry? | Action |
| --- | --- | --- | --- |
| 400 | `MISSING_FIELD`, `INVALID_VALUE_FOR_FIELD`, `INVALID_URN_TYPE`, `INVALID_URN_ID`, `FIELD_LENGTH_TOO_LONG`, `INVALID_VALUE_BLANK_FIELD`, `VERSION_MISSING`, `EXPIRED_UPLOAD_URL`, `MEDIA_ASSET_PROCESSING_FAILED` | No | Permanent; fix request. `EXPIRED_UPLOAD_URL` → re-initialize upload. |
| 400 | `MEDIA_ASSET_WAITING_UPLOAD` | After fixing | Upload not finished; complete upload / wait, then retry create |
| 401 | Expired, invalid or revoked token; `EMPTY_ACCESS_TOKEN` | Once, after refresh | Refresh the access token and retry once. If refresh fails or token was revoked by the member → account needs interactive re-authorization (operator action) |
| 403 | `ACCESS_DENIED` — missing scope, missing Page role, product not approved | No | Permanent until permissions/roles change |
| 404 | `NOT_FOUND` (can also mask a restricted API) | No | |
| 405 / 411 | Method not allowed / length required | No | Client bug |
| 409 | `CONFLICT` — write conflict | Yes | Docs: "Retry the request" (backoff). For create, check for an existing post first where possible |
| 413 / 415 | Too large / unsupported media type (asset uploads) | No | |
| 422 | `UNPROCESSABLE_ENTITY` — semantic error; reported by third parties for duplicate content | No | Treat as permanent; surface message |
| 426 | `NONEXISTENT_VERSION` — version header sunset | No | Deployment/config fault: bump `Linkedin-Version`; alert operator |
| 429 | `TOO_MANY_REQUESTS` | Yes, delayed | Backoff; if still throttled, defer until after 00:00 UTC |
| 500 | `INTERNAL_SERVER_ERROR` (also returned for "access token downstream verification failures") | Yes | Backoff with cap. **Ambiguous for create** — see idempotency |
| 503 | `SERVICE_UNAVAILABLE` | Yes | Backoff |
| 504 | Gateway timeout | Yes | Backoff. **Ambiguous for create** |
| network timeout / connection reset | — | Careful | Outcome unknown for `POST /rest/posts`; do not blindly retry |

---

## Token lifecycle

| Token | Lifetime | Notes |
| --- | --- | --- |
| Authorization code | 30 minutes | |
| Access token | **60 days** (`expires_in` = 5184000) — "Currently, all access tokens are issued with a 60-day lifespan" | "LinkedIn does not generate long-lived access tokens" |
| Refresh token | **365 days**, fixed from the original consent | Only for eligible apps |
| 2-legged app token | 30 minutes | Not usable for posting |

Refresh tokens:

- "LinkedIn supports programmatic refresh tokens for all approved Marketing Developer Platform (MDP) partners"; the OAuth page says they are "available for a limited set of partners". When enabled, the code exchange returns `refresh_token` and `refresh_token_expires_in`.
- **UNVERIFIED** whether a Community Management API **Development-tier** app receives refresh tokens, and whether a self-serve Share on LinkedIn app ever does (the docs imply it does not). The implementation must branch on the presence of `refresh_token` in the token response rather than assume.
- Refresh request: `POST https://www.linkedin.com/oauth/v2/accessToken` with `grant_type=refresh_token`, `refresh_token`, `client_id`, `client_secret`.
- **The refresh token's TTL does not reset.** Refreshing yields a new 60-day access token, but the refresh token keeps counting down from the original 365 days (day 59 → 306 days left). Persist whatever `refresh_token` and `refresh_token_expires_in` the response returns.
- When the refresh token expires, "the member must reauthorize your application" — i.e. **a human must redo the browser consent at least once a year**, even in the best case.
- "LinkedIn reserves the right to revoke Refresh Tokens or Access Tokens at any time"; the expected fallback is the standard interactive OAuth flow. Members can also revoke the app from their LinkedIn settings.
- Failure signature: `400 invalid_request` "The provided authorization grant or refresh token is invalid, expired or revoked".

Without refresh tokens: the only documented renewal is sending the member through the authorization URL again. The consent screen is skipped only if the member is still logged in to linkedin.com in that browser **and** the current access token has not expired — still a browser round-trip, so not usable by an unattended container. Practical consequence: a member-posting account on a self-serve app needs a manual re-authorization about every 60 days.

Operational implications for an unattended service:

- One-time interactive setup per LinkedIn account (member), producing stored credentials: access token, expiry, refresh token (if any), refresh expiry, granted scopes, person URN, and for Pages the organization URN(s).
- Proactive refresh before `expires_in` elapses; never wait for a 401.
- Track and expose "re-authorization required by {date}" as account state; alert well ahead of the 60-day / 365-day cliffs.
- `POST /oauth/v2/introspectToken` returns `active`, `status` (`active` / `expired` / `revoked`), `expires_at`, `scope`, `auth_type` — usable as a health check.
- Tokens are secrets at rest (encrypted storage); size columns for ≥ 1,000 characters.

---

## Production requirements

### Member posting (self-serve)

- Developer app + "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect" products. Listed as open permissions "available to all developers" via self-service; no review step documented.
- HTTPS redirect URI registered on the app.

### Organization posting (Community Management API)

Vetted product with two tiers. "All applications start with Development tier."

Application prerequisites (Development tier):

- "Only available to registered legal organizations for commercial use cases only."
- Verified **business email** (personal addresses fail vetting), organization legal name, registered address, website and privacy policy.
- A super admin of the organization's LinkedIn Page must **verify the app** (associate it with the Page).
- App name/logo must not include LinkedIn or Microsoft names or logos (including "Linked" or "In").
- Use case must be an approved one (Page Management, Page Analytics, Profile Management, Employee Advocacy, etc.) and not a restricted one.
- **The Community Management API must be requested on a new app with no other API products**: "Only request Community Management API Development Tier access with new developer applications that don't have access to other API products" (otherwise the request option is greyed out). Consequence: member posting via Share on LinkedIn/OIDC and Page posting via Community Management API may need **two separate LinkedIn apps** (two client IDs/secrets) — although the Community Management API itself also grants `w_member_social` and `r_basicprofile`, which may make one app sufficient. **UNVERIFIED** whether "Sign In with LinkedIn using OpenID Connect" can coexist on a Community Management app.
- If rejected, you cannot re-apply with the same app; a new app is required.

Development tier restrictions:

- 500 API calls per app per 24 h; 100 per member per 24 h.
- `BATCH_GET` calls not allowed; social-action webhook push notifications disabled.
- Integration and testing must be completed **within 12 months** of provisioning.

Standard tier upgrade:

- Complete the Standard tier access form (under My Apps → Products), provide a company/product overview, use-case description, **test credentials**, and a **screen recording** demonstrating each declared use case (e.g. full OAuth consent, posting to a Page, how comments and commenter data are displayed).
- Reviewed for approved use case, valid privacy policy, and compliance with terms and data-storage requirements. Later functional changes "may require re-review".
- No review timeline is published (**UNVERIFIED** typical duration).

An operator-run internal publishing service (not a multi-tenant SaaS product with a UI) may be a poor fit for a review process built around a product demo. Whether LinkedIn approves single-organization internal tools is **UNVERIFIED** and is the main business risk for Page publishing.

### Versioning obligations

- Base path `https://api.linkedin.com/rest/`; every call must send `Linkedin-Version: YYYYMM`. "The latest version is not applied by default."
- New versions are published **monthly** (no 202512 version appears in the list) and each is supported for **a minimum of one year**, then sunset. Requests with a sunset version fail with `426 NONEXISTENT_VERSION`; a missing header fails with `400 VERSION_MISSING`.
- Sunset dates on the research date: 202609 → 2027-09-15; 202608 → 2027-08-17; 202601 → 2027-01-15; 202511 → 2026-11-16; 202510 → 2026-10-15.
- LinkedIn may ship patch releases with breaking changes inside the window for critical security/privacy/bug fixes.
- Consequence: the version must be configuration, pinned and tested, with a scheduled **at-least-annual upgrade task**. An unmaintained deployment stops working entirely within 12 months.

### Data handling

Marketing API data-storage requirements (apply to data retrieved through the APIs): authenticated members' person ID/URN and post/organization URNs — no restriction; members' social activity data — 48 hours; other members' profile data — 24-hour cache only; organizations' social activity data — six weeks (six months for an authenticated organization); Page admin/reporting data — one year. Content the service itself submits is the caller's own data; the limits matter if the service reads posts, comments or analytics back.

---

## Known restrictions

### Idempotency

- **No idempotency-key mechanism is documented** for `POST /rest/posts` or the media `initializeUpload` actions. Nothing in the Posts, Images, Videos, Documents, error-handling or versioning pages mentions a client-supplied request ID or dedupe token.
- A successful create returns only a header (`x-restli-id`) and an empty body. If the response is lost (timeout, connection reset, 5xx after commit), the caller does not know the post URN and cannot tell whether the post exists.
- Detecting a post created with a lost response:
  - **Organization author:** query `GET /rest/posts?q=author&author={encoded org URN}&count=10&sortBy=CREATED` (needs `r_organization_social`) and match on commentary/`createdAt`. Workable.
  - **Member author:** the same finder requires `r_member_social`, which is closed. **There is no supported way to list a member's posts**, so a lost response on a member post cannot be reconciled through the API. The outcome must be recorded as "unknown" and resolved by a human (or by accepting a possible duplicate/missed post).
- Duplicate-content rejection: **UNVERIFIED** in LinkedIn docs. Third-party integrator documentation reports that re-posting identical content is rejected with `422` (one source cites a ~12-hour window). If true, this acts as an accidental safety net against double-posting on retry, but it is undocumented, its window and matching rules are unknown, and it must not be relied on as an idempotency mechanism. The Posts API page documents `422 UNPROCESSABLE_ENTITY` only as a generic semantic error.
- `DELETE` is documented as idempotent (204 for already-deleted posts). Media `initializeUpload` is safe to repeat (it just allocates another asset). `finalizeUpload` repeat behaviour is **UNVERIFIED**.
- Recommendation for the service contract: persist an "attempt started" record before the create call, treat timeouts/5xx on create as *outcome unknown* rather than *failed*, and only auto-retry creates when the adapter can prove absence (organization author with read scope).

### Historical content and analytics

- `r_member_social` is **closed**: "We're not accepting access requests at this time due to resource constraints." Listing or reading a member's existing posts, comments and reactions is unavailable. The service can only know about member posts it created itself and whose URN it stored.
- Member post analytics are available without `r_member_social`, via `GET /rest/memberCreatorPostAnalytics` (`q=entity` for one post URN, or `q=me` for aggregate) with scope `r_member_postAnalytics` (Community Management API, versions 202506+). Metrics include `IMPRESSION`, `MEMBERS_REACHED`, `RESHARE`, `REACTION`, `COMMENT`, and from 202604 also `POST_SAVE`, `POST_SEND`, `LINK_CLICKS` and others; `TOTAL` or `DAILY` aggregation (not every metric supports `DAILY`). This is **not** available to a self-serve Share on LinkedIn app, and it cannot enumerate posts — the caller must already hold the URN.
- Organization analytics: `GET /rest/organizationalEntityShareStatistics?q=organizationalEntity&organizationalEntity={urn}` (optionally `shares=List(...)` / `ugcPosts=List(...)`), scope `rw_organization_admin`, Page role `ADMINISTRATOR`. Organic statistics only; returns data within a **rolling 12-month window**; no pagination; time-bound statistics are not supported for per-post queries.
- Organization post history is readable via the author finder with `r_organization_social` (and the three poster/admin Page roles).

### Other restrictions

- No URL scraping for link posts; preview title, description and thumbnail must be supplied by the caller.
- Media cannot be supplied by URL; bytes must be uploaded to LinkedIn.
- Post media/content cannot be edited after creation (only commentary, CTA label, landing page, lifecycle state, ad context).
- Organic carousels are not supported; multi-image posts cannot be sponsored.
- Mentions render only when the display text matches the entity name exactly.
- The image upload `PUT` requires the bearer token while the video part `PUT` must not include it — an easy source of bugs in a shared HTTP client.
- Member-only (`w_member_social`) tokens cannot poll `/rest/images` status on the versioned gateway.
- Community Management data may not be used for "social feed" display, advertising, sales or recruiting use cases, and member data may not be exported or combined with other data.
- "No Headless or Fake Accounts."
- `organizationAcls` responses use `organization` in some samples and `organizationTarget` in others; handle both.

### SDK assessment

Official beta clients exist under the `linkedin-developers` GitHub organization, both described as thin Rest.li clients and both carrying the warning "currently in beta and is subject to change … Use of this library is at your own risk":

| Library | Latest release | Last code push | State |
| --- | --- | --- | --- |
| `linkedin-api-client` (Python, PyPI) — `linkedin-developers/linkedin-api-python-client` | 0.3.0, 2023-06-29 | 2024-04-21 | Beta, not archived, 35 open issues |
| `linkedin-api-client` (npm) — `linkedin-developers/linkedin-api-js-client` | 0.3.0, 2023-02-07 | 2024-07-26 | Beta, not archived, 14 open issues |

Both are effectively unmaintained (no release in over three years, no push in over two) and do not cover the binary upload steps in any special way. **Recommendation: direct HTTP** with a small in-house client that handles headers, Rest.li URN encoding, `x-restli-id` extraction, multipart video upload and token refresh. The SDK choice therefore does not constrain the language/framework decision.

---

## Official documentation references

Every URL below was opened during this research pass (2026-10-09).

LinkedIn / Microsoft Learn — posting and media:

- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/post-api-schema
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/images-api
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/documents-api
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/multiimage-post-api
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/vector-asset-api
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/contentapi-migration-guide
- https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin
- https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2

Versioning, errors, rate limits:

- https://learn.microsoft.com/en-us/linkedin/marketing/versioning
- https://learn.microsoft.com/en-us/linkedin/marketing/integrations/migrations
- https://learn.microsoft.com/en-us/linkedin/marketing/error-responses
- https://learn.microsoft.com/en-us/linkedin/shared/api-guide/concepts/error-handling
- https://learn.microsoft.com/en-us/linkedin/shared/api-guide/concepts/rate-limits

Authentication:

- https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow
- https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens
- https://learn.microsoft.com/en-us/linkedin/shared/authentication/client-credentials-flow
- https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access
- https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection

Access programs, organizations, analytics, policy:

- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/community-management-overview
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management-app-review
- https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access
- https://learn.microsoft.com/en-us/linkedin/marketing/quick-start
- https://learn.microsoft.com/en-us/linkedin/marketing/restricted-use-cases
- https://learn.microsoft.com/en-us/linkedin/marketing/data-storage-requirements
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/organizations/organization-access-control-by-role
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/organizations/share-statistics
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/members/post-statistics
- https://developer.linkedin.com/product-catalog/marketing/community-management-api

SDK status:

- https://github.com/linkedin-developers/linkedin-api-python-client
- https://github.com/linkedin-developers/linkedin-api-js-client
- https://api.github.com/repos/linkedin-developers/linkedin-api-python-client
- https://api.github.com/repos/linkedin-developers/linkedin-api-js-client
- https://pypi.org/pypi/linkedin-api-client/json
- https://registry.npmjs.org/linkedin-api-client

Non-primary sources (search results only; used solely for the two items marked UNVERIFIED — commentary length and duplicate-content rejection — and not opened in full):

- https://developers.posteverywhere.ai/platforms/linkedin
- https://docs.publora.com/raw/platforms/linkedin
- https://help.oneupapp.io/en-us/article/failed-post-help-1pnirj2/

## Implementation in this service

Added 2026-10-09. The onboarding and the two open questions were researched again before implementation; see `docs/research/testing-and-onboarding.md`, section B.3. Code: `src/contavex/providers/linkedin/rest.py`.

| Topic | What the implementation does |
| --- | --- |
| API | Versioned `/rest/posts`, `/rest/images`, `/rest/videos`, `/rest/documents` only. The legacy `/v2/ugcPosts` is not implemented; if a self-serve app is refused on `/rest/posts` (403) that fallback has to be added |
| Account settings | `author` (`urn:li:person:…` or `urn:li:organization:…`, required), `api_version` (default `202609`; review yearly), `api_base`, `oauth_base` |
| Credentials | `access_token` (required); `client_id` + `client_secret` (to learn the token's expiry); `refresh_token` (used if LinkedIn issued one) |
| Content | Text; one image, video or document; 2 to 20 images. Media is uploaded as bytes; video in 4 MB parts with progress checkpointed |
| Text | Plain text is escaped for the "little" format so it renders as written; `#hashtags` stay hashtags. `text_format: "little"` sends the text as is, for mentions |
| Unconfirmed create | Page author: the author's recent posts are listed and matched on text; found means published, not found twice means it is tried again. Member author, or a Page token without read permission: `outcome_unknown` |
| Upload URLs | Bytes and the token are only ever sent to a host under `linkedin.com` |
| Token expiry | With a refresh token: renewed when under 14 days are left. Without: the expiry date is read by token introspection and shown on the account |
| Not implemented | Articles/link posts, polls, reshares, targeting, video captions and thumbnails, the interactive OAuth flow itself (tokens are obtained with LinkedIn's Token Generator) |
| Checked how | A stateful fake of the REST API, OAuth endpoints and upload host. Not yet run against LinkedIn itself |

### Live correction, 2026-10-14

Deleting a `urn:li:share` created by a self-serve app through `DELETE /rest/posts/{urn}` returned 404 while the post existed; `DELETE https://api.linkedin.com/v2/shares/{id}` (no `LinkedIn-Version` header) deleted it with 200. The implementation now uses that fallback for shares, and no longer treats a 404 from `/rest/posts` as success.
