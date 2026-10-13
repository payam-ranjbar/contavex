# Instagram integration research

Researched: 2026-10-09
Graph API version: **v26.0** (released 2026-07-29, latest per the Graph API changelog). The Instagram Platform reference pages still print `v25.0` in every example and "The latest version is: v25.0"; v25.0 is supported until 2028-07-29. Pin the version in config, default `v26.0`, and verify it against `graph.instagram.com` on first integration test (see UNVERIFIED list).

Scope: publishing to Instagram professional accounts **controlled by the operator** of the service. Multi-tenant onboarding is covered only as future work under "Production requirements".

Conventions: statements are taken from Meta pages listed under "Official documentation references". Anything not confirmed there is tagged **UNVERIFIED**. Where Meta's own pages contradict each other, both values are recorded.

---

## Authentication

### Supported APIs

The "Instagram Platform" is the only supported API family. It has two configurations, and an app must choose one ("Your app can either use Facebook Login or Instagram Login but not both" — App Review page).

| | Instagram API with **Instagram Login** | Instagram API with **Facebook Login** |
| --- | --- | --- |
| Authorization | Business Login for Instagram | Facebook Login for Business |
| Token type | Instagram User access token | Facebook User or Page access token |
| Host | `graph.instagram.com` | `graph.facebook.com` (+ `rupload.facebook.com` for resumable video upload) |
| Facebook Page link | **Not required** | **Required** (IG account must be connected to a Page; the user must be able to perform tasks on it) |
| Content publishing | Yes | Yes |
| Resumable (binary) video upload | No | Yes |
| `DELETE /{ig-media-id}` | No | Yes |
| Collaborators, product tagging, partnership-ads label, hashtag search | No ("cannot access ads or tagging"; overview marks collaborators "Facebook Login for Business only") | Yes |
| App ID / secret used | Instagram app ID + Instagram app secret (shown under Instagram > API setup with Instagram login) | Meta app ID + app secret |

The **Instagram Basic Display API is gone**: the Instagram Platform changelog records it as end-of-life on 2024-12-04, "all requests return errors". Personal (consumer) accounts cannot be reached by any API.

### Recommended path for an operator-owned account

**Instagram Login, no OAuth flow, long-lived token generated in the App Dashboard and refreshed by the service.**

Meta documents this case explicitly: "If your app serves only your Instagram professional accounts, or accounts you manage, you do not need to implement a login flow. However, you will need to configure the business login settings in the App Dashboard to obtain an Instagram app ID and an Instagram app secret, as well as obtain long-lived access tokens."

Steps:
1. Create a Meta app of type **Business**; add the **Instagram** product, "API setup with Instagram login".
2. App Dashboard > Instagram > API setup with Instagram business login > **Generate token** next to the account, log into Instagram, copy the token. Dashboard tokens are long-lived (60 days).
3. `GET https://graph.instagram.com/v26.0/me?fields=user_id,username,account_type` to obtain the IG ID (`user_id`) used in publishing paths.
4. Service refreshes the token on a schedule (see "Token lifecycle").

Choose **Facebook Login instead** only if the service must have one of: API deletion of published media, binary video upload without a public URL, collaborators / product tags / partnership labels. Because one app cannot use both logins, this is a deployment-time decision; the adapter should treat host, token kind and capability flags as configuration.

### Facebook Login variant (alternative)

1. Business app with products: Facebook Login for Business, Instagram > API setup with Facebook login.
2. Obtain a Facebook User token with the publishing permissions (Graph API Explorer works for the operator's own account), exchange for long-lived: `GET https://graph.facebook.com/v26.0/oauth/access_token?grant_type=fb_exchange_token&client_id=…&client_secret=…&fb_exchange_token=…`.
3. `GET /me/accounts` returns Pages with a Page access token and `tasks`; `GET /{page-id}?fields=instagram_business_account` returns the IG user ID.
4. Publish with the Page access token (content-publishing guide) or User token (reference pages) on `graph.facebook.com`.

### If an OAuth flow is ever needed (Instagram Login)

- Authorize: `https://www.instagram.com/oauth/authorize?client_id=…&redirect_uri=…&response_type=code&scope=instagram_business_basic,instagram_business_content_publish` (optional `state`, `force_reauth`, `enable_fb_login`). Code is valid 1 hour, single use; strip trailing `#_`.
- Code to short-lived token (1 hour): `POST https://api.instagram.com/oauth/access_token` with `client_id`, `client_secret`, `grant_type=authorization_code`, `redirect_uri`, `code`.
- Short-lived to long-lived (60 days): `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret=…&access_token=…`.

Requests may carry the token as `Authorization: Bearer <token>` (used in Meta's publishing examples) or `access_token` query parameter. The rupload host uses `Authorization: OAuth <token>`.

---

## Account requirements

- The Instagram account must be a **professional account**: Business or Creator (`account_type` returns `Business` or `Media_Creator`). Consumer accounts are not supported.
- Instagram Login: no Facebook Page needed. The person generating the token must be able to log into the Instagram account.
- Facebook Login: the IG account must be connected to a Facebook Page, and the token's user must be able to perform `MANAGE` or `CREATE_CONTENT` tasks on that Page (UI task names "Full control" / "Content").
- Facebook Login extras that can block publishing: Page Publishing Authorization (PPA) must be completed if the linked Page requires it (not detectable via API); if the Page requires two-factor authentication the Facebook user must have done 2FA.
- The Facebook Login landing page says stories publishing is restricted to business accounts (as summarised from that page); the `/media` reference does not repeat this restriction. Treat Creator-account story publishing as **UNVERIFIED**.
- The operator's developer account must have a role on the Meta app (required for Standard Access).

---

## Permissions

| Purpose | Instagram Login scope | Facebook Login permission |
| --- | --- | --- |
| Read profile/media, required base | `instagram_business_basic` | `instagram_basic` (+ `pages_show_list`) |
| Create containers, publish, read publishing limit, read container status | `instagram_business_content_publish` | `instagram_content_publish` + `pages_read_engagement` |
| Role on Page granted through Business Manager | n/a | additionally `ads_management` or `ads_read` |
| Delete published media | not available | `instagram_manage_contents` (+ `instagram_basic`) |
| Enable/disable comments on media | `instagram_business_manage_comments` | `instagram_manage_comments` |
| Product tagging | not available | `catalog_management`, `instagram_shopping_tag_products` |
| Partnership label | not available | `instagram_branded_content_creator` or `instagram_basic` |

Notes:
- The pre-2025 Instagram Login scope names (`business_basic`, `business_content_publish`, …) were deprecated on 2025-01-27; only the `instagram_business_*` names work.
- Permissions-reference dependencies: `instagram_business_content_publish` depends on `instagram_business_basic`; `instagram_content_publish` depends on `instagram_basic`, `pages_read_engagement`, `pages_show_list`.
- The App Review page spells two names as `instagram_content_publishing` / `instagram_business_content_publishing`; the scope strings used in OAuth and the endpoint references are `…_content_publish`.
- Minimum for this service on the recommended path: `instagram_business_basic`, `instagram_business_content_publish`.

---

## Publishing workflow

All paths below are relative to `https://graph.instagram.com/v26.0` (Instagram Login) or `https://graph.facebook.com/v26.0` (Facebook Login). `{ig-id}` is the professional account ID.

### 1. Create a container — `POST /{ig-id}/media`

Returns `{"id": "<IG_CONTAINER_ID>"}` (plus `uri` for resumable uploads). Meta fetches the media from the URL you pass ("We cURL media used in publishing attempts").

| Post kind | Required parameters |
| --- | --- |
| Feed image | `image_url` |
| Reel | `media_type=REELS`, `video_url` |
| Story (image) | `media_type=STORIES`, `image_url` |
| Story (video) | `media_type=STORIES`, `video_url` |
| Carousel child | `image_url` or (`media_type=VIDEO` + `video_url`), plus `is_carousel_item=true` |
| Carousel parent | `media_type=CAROUSEL`, `children=<id1>,<id2>,…` |

Optional parameters:

| Parameter | Applies to | Notes |
| --- | --- | --- |
| `caption` | image, reel, carousel parent | Max 2200 characters, 30 hashtags, 20 @ tags. Not supported on carousel children. |
| `alt_text` | single image, image child of carousel | Up to 1000 characters. Not for reels or stories. |
| `location_id` | image, reel, carousel parent | ID of a Facebook Page that has location data; otherwise fails with `INSTAGRAM_PLATFORM_API__INVALID_LOCATION_ID`. Not on carousel children. |
| `user_tags` | image, video, story | Array of `{username, x, y}`; `x`/`y` 0.0–1.0, required for images, optional for stories; public users only. |
| `collaborators` | feed image, reel, carousel | Up to 3 usernames. Not for stories. Facebook Login only per overview. |
| `share_to_feed` | reel | `true` = may appear in Feed and Reels tabs; `false` = Reels tab only. |
| `cover_url` | reel | Public JPEG URL; wins over `thumb_offset`. |
| `thumb_offset` | video, reel | Milliseconds; default 0. |
| `audio_name` | reel | Can be renamed only once. |
| `trial_params` | reel | `{graduation_strategy: MANUAL \| SS_PERFORMANCE}`. |
| `is_ai_generated` | all except carousel children | Set on the carousel parent only; setting on children errors. |
| `product_tags`, `branded_content_sponsor_ids`, `is_paid_partnership` | see reference | Facebook Login only. |
| `upload_type=resumable` | video kinds | Facebook Login only; see below. |

Meta "strongly recommend[s]" URLs containing only US-ASCII characters "or the request will fail". List/dict parameters (`user_tags`, etc.) must be URL-encoded when sent as query parameters; a JSON body with `Content-Type: application/json` is shown in Meta's examples.

### 2. Wait until the container is ready — `GET /{container-id}?fields=status_code,status`

`status_code` values:

| Value | Meaning |
| --- | --- |
| `IN_PROGRESS` | Still processing |
| `FINISHED` | Ready to publish |
| `PUBLISHED` | Media object has been published |
| `ERROR` | Processing failed; `status` then carries an error subcode |
| `EXPIRED` | Not published within 24 hours |

Meta's recommended cadence: "once per minute, for no more than 5 minutes". "Video uploads are asynchronous, so receiving a container ID does not guarantee that the upload was successful." Publishing a container that is not ready fails with code 9007 / subcode 2207027. Images are normally ready immediately, but polling before publish is the safe uniform rule. Other container fields: `copyright_check_status` (`status`: `completed|error|in_progress|not_started`, `matches_found`), and on `graph.facebook.com` `video_status` (`uploading_phase`, `processing_phase`).

### 3. Publish — `POST /{ig-id}/media_publish` with `creation_id=<container id>`

Returns `{"id": "<IG_MEDIA_ID>"}`. Follow with `GET /{ig-media-id}?fields=id,permalink,media_type,timestamp` (the `media_product_type` field is documented as Facebook Login only).

### Carousels

1. Create each child container with `is_carousel_item=true` (images and videos may be mixed; reels cannot be children).
2. Wait for every video child to reach `FINISHED`.
3. Create the parent: `media_type=CAROUSEL`, `children` = comma-separated child IDs, `caption` etc. on the parent.
4. Publish the parent ID. Error subcode 2207028 states the valid size is 2–10 items.

### Resumable upload (Facebook Login only)

1. `POST https://graph.facebook.com/v26.0/{ig-id}/media` with `upload_type=resumable` and `media_type=REELS|STORIES|VIDEO` (no `video_url`). Response: `{"id": …, "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/<container id>"}`.
2. Upload bytes: `POST https://rupload.facebook.com/ig-api-upload/v26.0/{container-id}` with headers `Authorization: OAuth <token>`, `offset: 0`, `file_size: <bytes>` and the file as the raw body. Alternative: header `file_url: <public url>` instead of a body. Response `{"success": true, "message": "Upload successful."}`; failures return `debug_info` with a `retriable` boolean.
3. To resume after an interruption, read `video_status.uploading_phase.bytes_transferred` from the container and re-POST with that `offset`.
4. Poll `status_code`, then `media_publish` as usual.

This is the **only** documented way to publish without a public URL, and it covers **video only**.

### Container lifetime

Containers expire after 24 hours. An account can create at most 400 containers per rolling 24 hours.

---

## Supported media

### Feed image

| Property | Requirement |
| --- | --- |
| Format | **JPEG only** (MPO and JPS not supported). PNG/WebP/GIF/HEIC must be converted before publishing. |
| File size | 8 MB maximum |
| Aspect ratio | 4:5 to 1.91:1 |
| Width | min 320 (scaled up), max 1440 (scaled down) |
| Color space | sRGB (others converted) |

### Reel (also the spec for video in general)

| Property | Requirement |
| --- | --- |
| Container | MOV or MP4 (MPEG-4 Part 14), no edit lists, moov atom at front |
| Video codec | HEVC or H264, progressive scan, closed GOP, 4:2:0 chroma |
| Audio codec | AAC, 48 kHz max, 1 or 2 channels, 128 kbps |
| Frame rate | 23–60 fps |
| Picture size | max 1920 horizontal pixels |
| Aspect ratio | 0.01:1 to 10:1; 9:16 recommended |
| Video bitrate | VBR, 25 Mbps max |
| Duration | 3 seconds to 15 minutes |
| File size | 300 MB max |
| Cover photo | JPEG, 8 MB max, sRGB, 9:16 recommended (centre-cropped; 1:1 crop when shared to feed) |

`share_to_feed` and Reels-tab eligibility: Meta warns that neither value guarantees the reel appears in the Reels tab. After publishing, a reel's `media_type` reads back as `VIDEO`.

### Story

| Property | Image | Video |
| --- | --- | --- |
| Format | JPEG, 8 MB max, sRGB | MOV/MP4, same codec rules as reels |
| Aspect ratio | 9:16 recommended | 0.1:1 to 10:1; 9:16 recommended |
| Duration | — | 3 to 60 seconds |
| File size | 8 MB | 100 MB max |

Stories expire after 24 hours. No stickers (link, poll, location). No caption, alt text or collaborators documented for stories; `user_tags` mentions are supported. Stories are not returned by `GET /{ig-id}/media`; use `GET /{ig-id}/stories`.

### Carousel

- 2 to **10** items (images, videos, or mixed). The API docs give 10 in three places; a 20-item limit is not documented for the API.
- All images are cropped to the aspect ratio of the first image (default 1:1).
- Reels cannot be children; caption and location go on the parent only; alt text goes on image children.
- Counts as one post against the publishing limit.

### Not supported through the API

Shopping tags (outside the product-tagging feature), filters, story stickers, music tagging other than original audio, scheduling (no native scheduled-publish parameter is documented), editing after publish.

---

## Limits

| Limit | Value | Source |
| --- | --- | --- |
| API-published posts per account per 24-hour moving window | **100** per content-publishing guide; **50** per `media_publish` reference, carousel section, and `content_publishing_limit` reference (`quota_total` "currently 50") | Meta pages contradict each other |
| Carousel accounting | One carousel = one post | content-publishing guide |
| Containers created per account | 400 per rolling 24 hours | `/media` reference |
| Container lifetime | 24 hours | `/media` reference |
| Caption | 2200 characters, 30 hashtags, 20 @ tags | `/media` reference |
| Alt text | 1000 characters | `/media` reference |
| Collaborators | 3 | `/media` reference |
| Product tags | 5 | `/media` reference |
| Sponsor tags | 2 | content-publishing guide |
| `GET /{ig-id}/media` | Most recent 10K media | `/media` reference |

Do not hard-code 50 or 100. Read the live value:

`GET /{ig-id}/content_publishing_limit?fields=quota_usage,config`

```json
{ "data": [ { "quota_usage": 2, "config": { "quota_total": 50, "quota_duration": 86400 } } ] }
```

`quota_usage` counts containers published in the last 24 hours (or since the optional `since` Unix timestamp, no older than 24 h). The limit is enforced at `media_publish`; exceeding it yields code 9 / subcode 2207042. Meta recommends the app enforce the limit itself, especially when scheduling. Whether story posts count toward this quota is **UNVERIFIED**.

---

## Rate limits

- All Instagram Platform endpoints used here fall under **Business Use Case (BUC) rate limiting** (only Business Discovery and Hashtag Search use Platform rate limits).
- Formula: `Calls within 24 hours = 4800 * Number of Impressions`, where impressions = times any content from the account entered a person's screen in the last 24 hours. Counted per app + app-user pair over a rolling 24-hour window. A documented floor for accounts with near-zero impressions was not found (**UNVERIFIED**).
- Response header `X-Business-Use-Case-Usage`: JSON keyed by business/account ID, each entry with `type` (e.g. `instagram`), `call_count`, `total_cputime`, `total_time` (percentages of quota) and `estimated_time_to_regain_access` (minutes).
- Response header `X-App-Usage` (platform limits): `call_count`, `total_cputime`, `total_time` percentages. Platform formula: `200 * Number of Users` calls per hour.
- Throttling error codes: `4` (app), `17` (user), `32` (Pages API), `613` (custom limit), `80002` (Instagram BUC), `80001` (Page BUC with page/system token), others in 80000–80009.
- Guidance: when throttled, stop calling (continued calls extend the wait), spread calls evenly, watch the headers. A publish costs roughly 3–8 calls (container, a few polls, publish, read-back), so the content publishing limit binds long before BUC limits for a normal account.

---

## Errors

### Format

HTTP 400 for most Instagram errors (403 for some permission errors):

```json
{
  "error": {
    "message": "The image size is too large.",
    "type": "OAuthException",
    "code": 36000,
    "error_subcode": 2207004,
    "is_transient": false,
    "error_user_title": "Image size too large",
    "error_user_msg": "The image is too large to download. It should be less than 8 MiB.",
    "fbtrace_id": "A6LJylpZRKw2xKLFsAP-cJh"
  }
}
```

Classify on `code` + `error_subcode`; log `fbtrace_id`; keep `error_user_msg` for operators. `is_transient` appears in Meta's Instagram error samples but is not described in the Graph error-handling field list, so use it as a hint, not as the sole retry signal. The rupload host uses a different shape: `{"debug_info": {"retriable": false, "type": "…", "message": "…"}}`.

### Retry classification

| Error (code / subcode) | Meaning | Retryable? | Action |
| --- | --- | --- | --- |
| `1`, `2` | API unknown / API service (temporary) | Yes | Backoff retry. For `media_publish`, reconcile first (see Known restrictions). |
| `-1` / 2207001 | Instagram server error | Yes | Backoff retry |
| `-1` / 2207032 | "Create media fail, please try to re-create media" | Yes | Create a new container |
| `-1` / 2207053 | Unknown upload error (video) | Yes | New container, re-upload |
| `-2` / 2207003 | Meta timed out downloading the media | Yes | Retry; check media host latency |
| `-2` / 2207020 | Container expired | Yes, with new container | Recreate container |
| `24` / 2207008 | Container ID not found / not usable | Limited | Retry once, then new container |
| `24` / 2207006 | Media not found | No (usually) | Check permissions; new container |
| `9007` / 2207027 | Media not ready for publishing | Yes | Keep polling `status_code` until `FINISHED`, then publish |
| `9004` / 2207052 | Media could not be fetched from URI | Conditional | Retry only if the URL was transiently unreachable; otherwise fix URL/accessibility |
| `4`, `17`, `32`, `613`, `80002` | Rate limited | Yes, delayed | Wait `estimated_time_to_regain_access`; stop all calls for that account meanwhile |
| `9` / 2207042 | Publishing limit reached | Yes, much later | Defer until quota frees (rolling 24 h); check `content_publishing_limit` |
| `4` / 2207051 | Activity restricted as possible spam | No (automatic) | Hold; operator review. Same top-level code as app throttling, so always inspect the subcode. |
| `25` / 2207050 | Instagram account restricted | No | Operator must resolve in the Instagram app |
| `368` | Policy violation / temporarily blocked | No (automatic) | Meta says "wait and retry"; treat as a hold needing operator attention |
| `190` (subcodes 460 password changed, 463 expired, 467 invalid), `102` | Token expired/invalid/revoked | No | Mark credential invalid, alert; operator generates a new token |
| `190` / 458 | App not installed / authorization removed | No | Re-authorize |
| `10`, `200`–`299`, `3` | Permission missing or removed | No | Fix scopes / roles / access level |
| `100` / 2207023, 2207028, 2207040, 2207035–2207037 | Invalid parameter: media type, carousel size, too many tags, product tags | No | Reject request as validation failure |
| `36000` / 2207004 | Image over 8 MiB | No | Re-encode |
| `36001` / 2207005 | Image format not supported | No | Convert to JPEG |
| `36003` / 2207009 | Aspect ratio outside 4:5–1.91:1 | No | Crop/pad |
| `36004` / 2207010 | Caption too long | No | Shorten |
| `352` / 2207026 | Video format not supported | No | Transcode to MP4/MOV per spec |
| `1` / 2207057 | Invalid `thumb_offset` | No | Fix value |
| `-1` / 2207073 | Media type not supported (delete endpoint) | No | — |
| `506` | Duplicate post (generic Graph code) | No | Do not resend unchanged |
| Container `status_code=ERROR` | Processing failed | Depends on `status` subcode | Map the subcode with this table; default to permanent |
| Container `status_code=EXPIRED` | 24 h elapsed | Yes, with new container | Recreate |
| Network timeout / connection reset on `POST /media` | Unknown outcome | Yes | Safe to repeat: an orphan container just expires (counts toward 400/day) |
| Network timeout / connection reset on `POST /media_publish` | Unknown outcome | **Not blindly** | Reconcile first (Known restrictions) |

---

## Token lifecycle

### Instagram Login (recommended path)

| Token | Lifetime | How obtained |
| --- | --- | --- |
| Short-lived Instagram User token | 1 hour | OAuth code exchange at `api.instagram.com/oauth/access_token` |
| Long-lived Instagram User token | 60 days (`expires_in` ≈ 5183944) | `GET graph.instagram.com/access_token?grant_type=ig_exchange_token`, or **Generate token** in the App Dashboard (already long-lived) |
| Refreshed long-lived token | 60 days from refresh | `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=<current>` |

Refresh rules: token must be at least 24 hours old, not expired, and carry `instagram_business_basic`. "Tokens that have not been refreshed in 60 days will expire and can no longer be refreshed" — recovery is manual (dashboard or login flow). The refresh response contains an `access_token`; store whatever is returned rather than assuming the string is unchanged.

**Simplest sustainable strategy for an unattended service:**
1. Operator pastes the dashboard-generated long-lived token once (bootstrap secret).
2. The service persists the token and its expiry in **writable durable storage** (not only an env var, since the value can rotate), encrypted at rest.
3. A scheduled job refreshes when the token is older than 24 h and has, say, less than 30 days left (weekly is ample); on success, atomically replace token + expiry.
4. Alert when remaining lifetime drops below a threshold (e.g. 10 days) or on any `190` error; expose token expiry in health/status output.
5. On `190`, stop publishing for that account and require operator re-bootstrap.

### Facebook Login

| Token | Lifetime |
| --- | --- |
| Short-lived User token | about 1–2 hours |
| Long-lived User token | about 60 days (`fb_exchange_token`); cannot be exchanged once expired |
| Page token derived from a long-lived User token | "do not have an expiration date"; invalidated only under certain conditions |
| System User token (Business portfolio) | Either never-expiring or 60-day (`set_token_expires_in_60_days=true`, refreshable via `oauth/access_token` with `fb_exchange_token`); Meta now says "All integrations should adopt expiring tokens" |

System User tokens are generated in Business settings > System Users or via `POST /{system-user-id}/access_tokens` (`business_app`, `scope`, `appsecret_proof`). Limits: 1 system user + 1 admin system user at Standard Access. Meta describes them for "Ad objects or Pages". The Instagram references name only User/Page tokens, so **using a System User token for Instagram publishing is UNVERIFIED** against Meta docs (it is a common practice; test before relying on it). The documented non-expiring option for Facebook Login is the long-lived Page token.

Meta's general warning: "Do not depend on these lifetimes remaining the same — they may change without warning or expire early."

### What invalidates tokens

Confirmed via the Graph error-handling subcodes: password change (460), expiry (463), app authorization removed (458), user checkpointed (459), explicit revocation (`oauth/revoke` for system users), generic invalid/revoked (467). The dedicated Meta page on token expiry causes could not be retrieved (404), so a full list is **UNVERIFIED**; additional commonly cited triggers are losing the role on the Page/app and app secret reset.

---

## Production requirements

### For the operator-owned deployment (now)

- **Meta developer account** and a **Business-type app**.
- **Standard Access is sufficient**; **App Review is not required** and Business Verification is not required. Meta's scenario table: "My app is only for a business I own or manage. — No login or Instagram Login — Standard Access — Not required" (same row for Facebook Login). Business apps get Standard Access to all their permissions automatically.
- Standard Access permissions can only be granted by users who have a **role on the app**, so the person who generates the token must be an app admin/developer/tester (and, for Instagram Login, the Instagram account is added under the app's Instagram API setup).
- **App mode:** Development mode allows permissions from role users, which fits this case. Two caveats to test before go-live: (a) Meta's app-modes page says data generated in Development mode "such as test posts, can only be seen by role users" — whether this ever affects Instagram posts published through the API is **UNVERIFIED**, so confirm a test post is publicly visible, or switch the app to Live; (b) webhooks are delivered only to apps in Live mode.
- Operational needs: public HTTPS media hosting (see Known restrictions), durable encrypted token storage, a refresh job, local enforcement of the publishing quota, persisted publish state machine (container ID, status, media ID), monitoring of `X-Business-Use-Case-Usage`, alerting on `190`/`368`/account-restricted errors, a pinned Graph version with a calendar reminder for the version's end date.
- **Versioning policy:** each version is usable for at least 2 years ("a version will no longer be usable two years after the date that the subsequent version is released"); calls to an expired version are served by the next oldest usable version; unversioned calls use the app's dashboard default. Releases in the last two years: v21.0 2024-10-02, v22.0 2025-01-21, v23.0 2025-05-29, v24.0 2025-10-08, v25.0 2026-02-18, v26.0 2026-07-29 (about 2–3 per year; no fixed cadence is documented). v26.0 end date is TBD.

### SDK versus direct HTTP

- Official SDKs exist and are current: `facebook-python-business-sdk` (API `v26.0`, SDK `v26.0.2`, last commit 2026-09-17) and `facebook-nodejs-business-sdk` (`26.0.2`, last commit 2026-09-18). Both are auto-generated ("autogenerated from SDK Codegen") and are primarily the Marketing API SDK with other objects bundled.
- They do expose the endpoints: Python `IGUser.create_media`, `create_media_publish`, `get_content_publishing_limit` (parameter list includes `alt_text`, `collaborators`, `is_ai_generated`, `trial_params`, `upload_type`); Node `IGUser.createMedia`, `createMediaPublish`, `getContentPublishingLimit`.
- Gaps: they are built around `graph.facebook.com` (no first-class `graph.instagram.com` / Instagram Login support in the API client, **UNVERIFIED** beyond source inspection of the Node client's `GRAPH` constant), no helper for the rupload protocol, no polling/state handling, no Instagram token refresh, and a large ads-oriented dependency surface.
- **Recommendation: direct HTTP.** The integration is 5 endpoints with simple JSON; a thin client gives control over host selection, version pinning, timeouts, header capture and error mapping. Use the SDK source only as a parameter reference. Meta's sample `fbsamples/reels_publishing_apis` (linked from the docs) is a useful reference implementation.

### Future: letting external customers connect their own accounts (multi-tenant)

- **Advanced Access** for each permission (`instagram_business_basic`, `instagram_business_content_publish`, or the Facebook Login equivalents), which requires **App Review** and **Business Verification**. Meta's "Tech Provider … serves multiple businesses" scenario: App Review required for both login types.
- App Review deliverables: app icon (1024x1024), privacy policy URL, app category, business email, step-by-step reviewer instructions and test credentials, an English screencast per permission showing the end-to-end flow, at least one successful API call per permission, a visible login button that follows brand guidelines. The app must be externally loadable. A headless service therefore needs a small customer-facing web UI. (The "private apps" exemption only covers `instagram_basic` and `instagram_manage_comments`, not publishing.)
- App switched to **Live** mode.
- Full **OAuth onboarding**: authorize URL, redirect URI allow-list, `state` CSRF protection, code exchange, long-lived exchange, per-tenant IG ID discovery; re-consent flow when tokens die. Web is the only platform supporting Instagram Login.
- **Per-tenant token storage** (encrypted, with expiry, scopes and status), per-tenant refresh scheduling, per-tenant quota and rate-limit tracking (BUC limits are per app-user pair), per-tenant error isolation.
- **Data deletion**: privacy policy must explain deletion; implement the Data Deletion Request callback (HTTPS endpoint receiving a `signed_request`, returning `{ url, confirmation_code }`) and a deauthorize callback.
- Webhooks at Advanced Access with Business Verification if comment/mention features are added.
- Compliance with Meta Platform Terms and Developer Policies, including periodic data-use checkups (**UNVERIFIED** detail, not opened in this research).
- Decide the login type up front: one app cannot use both, and it determines which tenants can connect (Instagram-only accounts vs Page-linked accounts) and which features exist (delete, resumable upload, collaborators).

---

## Known restrictions

### Media must be publicly reachable

- Images, video (`video_url`), and `cover_url` are fetched by Meta from the URL you pass: "the media must be hosted on a publicly accessible server at the time of the attempt." There is **no binary/multipart upload for images** on either login type.
- Video can be uploaded as bytes only through resumable upload, which is **Facebook Login only**. On the recommended Instagram Login path every asset needs a public URL.
- Design consequence: the service needs a media staging component. Callers either pass an already-public HTTPS URL, or pass bytes/internal references that the service places on internet-reachable storage (object-storage bucket with public or pre-signed URLs, or a public reverse-proxied path). The URL must stay valid until the container reaches `FINISHED` (and, to be safe, until publish succeeds); use ASCII-only URLs; no auth headers, no private networks, no localhost. Whether Meta's fetcher follows redirects or accepts pre-signed query strings is **UNVERIFIED**; test it. Failures surface as 9004/2207052 (cannot fetch) or -2/2207003 (download too slow).
- The service should validate and normalise media before creating a container (JPEG conversion, size, aspect ratio, video codec/moov atom), because Meta rejects rather than converts.

### Idempotency and duplicate risk

- **No idempotency key exists.** Neither `/media` nor `/media_publish` documents any client token or dedup parameter.
- `POST /media` is safe to repeat: duplicates are unpublished containers that expire after 24 h (cost: the 400/day container quota).
- `POST /media_publish` is **not** safe to repeat blindly. If the response is lost, reconcile before retrying:
  1. `GET /{container-id}?fields=status_code`. `PUBLISHED` means the post exists; `FINISHED` means it was not published and publish may be retried; `IN_PROGRESS` means wait.
  2. If `PUBLISHED`, recover the media ID by listing `GET /{ig-id}/media?fields=id,caption,timestamp,permalink,media_type` (supports `since`/`until`) and matching on timestamp and caption; for stories use `GET /{ig-id}/stories`. The IG Container object documents no field that returns the resulting media ID, so this match is heuristic.
- Cannot be guaranteed: exactly-once publishing; deterministic mapping from container to media ID after a lost response; behaviour of a second `media_publish` on an already-`PUBLISHED` container (not documented, **UNVERIFIED** — do not rely on it being rejected); read-after-write consistency of `status_code` immediately after a timeout. Residual duplicate risk is small if the service persists the container ID **before** calling publish, uses one container per logical post, never creates a second container for a post whose first container's outcome is unknown, and serialises publishes per account.
- The service's own idempotency key must therefore be implemented locally (request key → stored container ID → stored media ID).

### Edit and delete

- **Caption/media edit: not possible.** `POST /{ig-media-id}` supports exactly one field, `comment_enabled` (true/false). No caption, alt text, location or tag changes after publishing.
- **Delete: Facebook Login only.** `DELETE https://graph.facebook.com/v26.0/{ig-media-id}` with `instagram_basic` + `instagram_manage_contents` (added 2025-12-03). Supports non-ad posts, stories, reels and whole carousels; individual carousel children cannot be deleted. Returns `{"success": true, "deleted_id": "…"}`. Not available on `graph.instagram.com`, so on the recommended path deletion is manual in the Instagram app.
- Containers cannot be updated or deleted; abandon them and let them expire.

### Webhooks

- Instagram webhook fields: `comments`, `live_comments`, `mentions`, `messages`, `message_echoes`, `message_reactions`, `messaging_handover`, `messaging_optins`, `messaging_policy_enforcement`, `messaging_postbacks`, `messaging_referral`, `messaging_seen`, `response_feedback`, `standby`, `story_insights`.
- **None signals container readiness or publish completion.** Polling `status_code` is the only mechanism.
- Webhooks need the app in Live mode, a public HTTPS endpoint with a valid certificate, and `X-Hub-Signature-256` verification; the webhooks page lists Advanced Access and Business Verification among requirements. Not needed for publishing.

### Other

- No server-side scheduling: the service must hold scheduled posts and publish at the due time (containers live only 24 h, so create them near publish time).
- Publishing limit documentation is inconsistent (50 vs 100); trust `content_publishing_limit`.
- Instagram reference pages lag the Graph version (show v25.0 while v26.0 is current).
- Carousel images are cropped to the first image's aspect ratio.
- Captions read back without the `@` symbol unless the user has admin-equivalent tasks; several read fields are Facebook Login only.
- `GET /{ig-id}/media` result ordering is not configurable.
- PPA and Page 2FA can silently block publishing on the Facebook Login path.

### UNVERIFIED summary

1. `v26.0` accepted on `graph.instagram.com` for the publishing endpoints (docs show v25.0).
2. Real per-account publishing quota (50 vs 100) and whether stories count.
3. System User tokens for Instagram publishing.
4. Complete list of token invalidation causes (Meta page returned 404).
5. Visibility of posts published while the app is in Development mode.
6. Behaviour of repeated `media_publish` on a `PUBLISHED` container.
7. Meta fetcher behaviour with redirects / pre-signed URLs; minimum BUC quota for zero-impression accounts.
8. Story publishing on Creator accounts.
9. SDK support for `graph.instagram.com`.

---

## Official documentation references

Meta serves a plain-markdown copy of each page by appending `.md` to the `/documentation/...` path; those copies were read directly.

Instagram Platform
- https://developers.facebook.com/docs/instagram-platform/overview
- https://developers.facebook.com/documentation/instagram-platform/overview.md
- https://developers.facebook.com/documentation/instagram-platform/llms.txt
- https://developers.facebook.com/docs/instagram-platform/content-publishing
- https://developers.facebook.com/documentation/instagram-platform/content-publishing.md
- https://developers.facebook.com/documentation/instagram-platform/content-publishing/resumable-uploads.md
- https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login
- https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/get-started.md
- https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login
- https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login.md
- https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login
- https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/get-started.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media_publish.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit.md
- https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-container
- https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/error-codes
- https://developers.facebook.com/documentation/instagram-platform/reference/instagram-media.md
- https://developers.facebook.com/documentation/instagram-platform/reference/access_token.md
- https://developers.facebook.com/documentation/instagram-platform/reference/refresh_access_token.md
- https://developers.facebook.com/documentation/instagram-platform/app-review.md
- https://developers.facebook.com/documentation/instagram-platform/webhooks.md
- https://developers.facebook.com/docs/instagram-platform/changelog
- https://developers.facebook.com/documentation/instagram-platform/create-an-instagram-app.md (stub only; redirects to the Meta App Development docs)

Graph API, tokens, access
- https://developers.facebook.com/docs/graph-api/changelog
- https://developers.facebook.com/docs/graph-api/guides/versioning
- https://developers.facebook.com/docs/graph-api/overview/rate-limiting
- https://developers.facebook.com/docs/graph-api/guides/error-handling
- https://developers.facebook.com/docs/graph-api/overview/access-levels
- https://developers.facebook.com/documentation/development/build-and-test/app-modes.md
- https://developers.facebook.com/documentation/development/create-an-app/app-dashboard/data-deletion-callback.md
- https://developers.facebook.com/documentation/facebook-login/guides/access-tokens.md
- https://developers.facebook.com/documentation/facebook-login/guides/access-tokens/get-long-lived.md
- https://developers.facebook.com/docs/business-management-apis/system-users/overview
- https://developers.facebook.com/docs/marketing-api/system-users/install-apps-and-generate-tokens
- https://developers.facebook.com/docs/permissions

SDKs and samples
- https://github.com/facebook/facebook-python-business-sdk (plus `facebook_business/apiconfig.py`, `adobjects/iguser.py` on `main`)
- https://github.com/facebook/facebook-nodejs-business-sdk (plus `package.json`, `src/objects/ig-user.js`, `src/api.js` on `main`)
- https://api.github.com/repos/fbsamples/reels_publishing_apis (commit metadata only; sample linked from the content-publishing guide)

Attempted, not retrievable (404), nothing used from them
- https://developers.facebook.com/docs/facebook-login/guides/access-tokens/debugging
- https://developers.facebook.com/docs/facebook-login/guides/access-tokens/debugging-and-error-handling

## Implementation in this service

Added 2026-10-09, after the research above. Code: `src/content_fleet/providers/instagram/instagram_login.py`.

| Topic | What the implementation does |
| --- | --- |
| Variant | Two implementations of the platform. `instagram-login` (Instagram API with Instagram Login): verified live. `facebook-login` (Instagram API with Facebook Login, added 2026-10-12): the same flow on `graph.facebook.com` plus `DELETE /{media-id}`; no token refresh; tested against a fake only. Its binary video upload is not implemented |
| Delete | Not possible with `instagram-login`: a live `DELETE` on `graph.instagram.com` (v25.0 and v26.0) returned code 100, subcode 33, "Unsupported delete request". Meta's media reference lists delete under Facebook Login only, with `instagram_manage_contents`. With `facebook-login` the service deletes images, reels, stories and whole carousels |
| Account settings | `ig_user_id` (required), `api_version` (default `v25.0`, the version the Instagram reference pages document), `api_base` |
| Kinds | `post` (one image, or a carousel of 2 to 10 images and videos), `reel` (one video), `story` (one image or video, no caption) |
| Steps | stage uploads → create container(s) → poll until `FINISHED` → check the publishing quota → `media_publish` → read the permalink |
| Token | Sent as `Authorization: Bearer`, never in a URL, except the documented refresh call. Refreshed when at least 24 hours old and with under 30 days left; stored encrypted |
| Unconfirmed `media_publish` | The container's `status_code` is read: `PUBLISHED` means the post exists and its id is looked up among recent media by caption; `FINISHED` twice, 30 seconds apart, means it never happened and it is tried again. If the container cannot be read after five tries: `outcome_unknown` |
| Expired container | Rebuilt once, then the publication fails as retryable |
| Quota | `content_publishing_limit` is read before publishing; an exhausted quota defers for 30 minutes instead of spending an attempt |
| Not implemented | Location, user tags, collaborators, cover image, trial reels, converting non-JPEG images, validating aspect ratio or video codec before upload |
| Checked how | A stateful fake of the Graph API, and live on 2026-10-11 against a creator account in a Development-mode app, with uploads staged on a private Cloudflare R2 bucket: single image with caption, hashtags and alt text (12 s); carousel of two images and a video (66 s); reel with `share_to_feed` and `thumb_offset` (54 s); image story (9 s); video story (51 s). Every vendor call returned 200 on the first try and the staged objects were deleted afterwards. Read back from Instagram: the alt text was stored, the carousel has children IMAGE, IMAGE, VIDEO, the reel is `media_product_type: REELS`. Not exercised live: media by public URL, error paths, token refresh (the token must be 24 hours old), the recovery after an unconfirmed publish |
