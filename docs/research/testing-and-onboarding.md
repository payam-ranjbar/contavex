# Research notes — vendor test facilities, credential onboarding, S3 staging, Instagram request shapes

Researched: 2026-10-09

Scope: complements `docs/INTEGRATIONS/{telegram,instagram,linkedin}.md`. Nothing from those files is repeated
unless it is needed to state a correction. Every statement comes from a page listed under "Sources consulted"
or from a local check described inline. **UNVERIFIED** = not confirmed in a primary source today.

Conventions: "local check" = executed on this machine today (Python 3.14.0, Windows 11) in a scratch directory;
no project file other than this one was touched.

---

## A. Test facilities per vendor

### A.0 Summary

| Vendor | Official sandbox for publishing? | Official machine-readable API description? | Practical safe target |
| --- | --- | --- | --- |
| Telegram | **Yes** — a separate test environment (test DC), same Bot API, different accounts and bots | **No.** Community JSON spec tracks 10.3 | Test-DC bot + test-DC channel, or a private channel on production |
| Instagram | **No.** No sandbox, no dry-run flag, no test Instagram accounts | **No** OpenAPI. A Meta Postman workspace exists (2023 link) | A second, private or throw-away **professional** Instagram account added to the app |
| LinkedIn | **No.** No sandbox, test companies are forbidden; up to five manual "test profiles" are permitted | **No** OpenAPI. Official Postman workspaces exist | A test member profile (visibility `CONNECTIONS`), delete after the run |

None of the three vendors publishes an OpenAPI/Swagger document. Unit and component tests therefore need
hand-written fakes; see A.5.

### A.1 Telegram

**Test environment (official).** `core.telegram.org/bots/features`, section "Testing your bot → Dedicated test
environment":

- It is a separate environment: you need a **new user account** created on the test servers and a **new bot**
  created there. Production tokens do not work on it and vice versa.
- Creating the test account (quoted from the page):
  - iOS: "tap 10 times on the Settings icon > Accounts > Login to another account > Test."
  - Telegram Desktop: "open ☰ Settings > Shift + Alt + Right click 'Add Account' and select 'Test Server'."
  - macOS: "click the Settings icon 10 times to open the Debug Menu, ⌘ + click 'Add Account' and log in via
    phone number."
- Creating the bot: while logged in to that test account, talk to @BotFather **inside the test environment**
  and run `/newbot` as usual ("create a new bot following the standard procedure").
- Request URL form: `https://api.telegram.org/bot<token>/test/METHOD_NAME`.
- The page also allows plain-HTTP links for `LoginUrl`/`WebAppInfo` in the test environment (not relevant here).
- `docs/INTEGRATIONS/telegram.md` already records that flood limits there "may at times be stricter".
- The Bot API reference page itself does not mention `/test/`; only the features page does.

What the operator needs: a phone number able to register on the test servers, a Telegram client with the
hidden test-server login, one test bot, one test channel with the bot as admin. Whether the test servers
accept an already-registered production phone number and how SMS codes are delivered there is
**UNVERIFIED** (not stated on the page).

Design consequence: the adapter needs one boolean (`test_environment`) that inserts `/test` between the token
and the method name. Nothing else changes.

**Self-hosted Bot API server (`tdlib/telegram-bot-api`) as a test target.**

- It is the real server in front of the real Telegram network, **not a mock**. Messages sent through it are
  real messages. It is useful to test large uploads and base-URL configurability, not to avoid posting.
- Needs `--api-id` / `--api-hash` (my.telegram.org), listens on plain HTTP port 8081, licence BSL-1.0,
  last push 2026-08-26.
- The registered command-line options (read from `telegram-bot-api.cpp` today) contain no test-DC switch.
  The test DC is selected **per request**, with the same URL form as the cloud API: `HttpConnection.cpp`
  does `url_path_parser.try_skip("/test")` and sets `is_test_dc`; `ClientManager.cpp` keys the bot on
  `token + "/test"`. So a local server can front the test environment too (source-derived, not documented).
- No official Docker image (already UNVERIFIED in the integration doc; unchanged).

**Official mock.** None exists. Telegram publishes no mock server and no schema.

**Private channel on production (practical default).** A private channel with no subscribers other than the
operator is a legitimate target: the bot is added as admin with `can_post_messages`, the numeric `-100…` id
is used as `chat_id`, and posts are deleted with `deleteMessage` afterwards. This exercises the production
path (limits, file handling) without an audience and needs only a production bot token.

**Machine-readable Bot API descriptions (all community, checked via GitHub API and raw files today).**

| Project | Format | Tracks | Last push | Licence | Note |
| --- | --- | --- | --- | --- | --- |
| `PaulSonOfLars/telegram-bot-api-spec` | Custom JSON (`api.json`, `api.min.json`): `methods{name, fields[{name, types, required, description}], returns}`, `types{…}` | **Bot API 10.3** (file header: "Bot API 10.3", "August 24, 2026"; 185 methods, 400 types) | 2026-08-25 | MIT | Scraped daily from core.telegram.org. Best current option. |
| `rockneurotiko/telegram_api_json` | Custom JSON | UNVERIFIED version | 2026-08-24 | none declared | Avoid: no licence. |
| `ark0f/tg-bot-api` | **OpenAPI** (`ark0f.github.io/tg-bot-api/openapi.json`) | **8.3.0** (file `info.version`) | 2025-02-14 | Apache-2.0 | Stale by seven minor versions despite the README's "nightly" claim. Do not use. |
| `sys-001/telegram-bot-api-versions` | Archive of schemas | — | 2024-01-05 | none | Stale. |
| apis.guru `telegram.org/5.0.0` | OpenAPI | 5.0 | — | — | Obsolete. |

Recommendation: vendor a pinned copy of `PaulSonOfLars/telegram-bot-api-spec/api.json` as a **test fixture
only** and add a contract test that checks every parameter name our client sends for the ~10 methods we use
exists in the spec, and that all `required: true` fields are sent. It does not describe error bodies.

**Third-party Telegram mocks.** `ptbtest` (python-telegram-bot), `telemulator3` (pyTelegramBotAPI) and
`tgtest` (Telethon end-to-end) were found; all are bound to a bot framework or to a real user session and
none is an HTTP-level fake usable with httpx2. Not recommended. Versions not examined.

### A.2 Meta / Instagram

**There is no sandbox for content publishing.** The content-publishing guide and the `/media`,
`/media_publish` and container references contain no test, sandbox, dry-run or "unpublished" parameter.
Every successful `media_publish` creates a real post on the real account.

| Facility | What it is | Useful here? |
| --- | --- | --- |
| **Development mode** | Default mode of a new app. "Apps in Development mode can only request permissions from role users". The app-modes page also says "Any data generated while an app is in Development mode, such as test posts, can only be seen by role users." | It is the mode this service runs in permanently (operator-owned account, Standard Access). The "only seen by role users" sentence is written for Facebook data; whether an Instagram post published through the API by a Development-mode app is hidden from the public is **UNVERIFIED** and must be observed on the first real post. Plan for the pessimistic case in both directions: treat every test publish as public, and verify public visibility before relying on Development mode for production. |
| **Test apps** | Child clones of a parent app, "always in Development mode", inherit settings at creation, max 50 per parent, created from the app selector → **Create Test App**. | Gives separate app id/secret for staging so the production app's settings are not touched. Does **not** make posts fake. |
| **Test users** | Simulated **Facebook** accounts (max 10/app). The page currently says: "We are temporarily removing the ability for apps to create new test users." They "can only interact with app-scoped test pages". | **No.** They are Facebook accounts, creation is suspended, and no Meta page describes test Instagram professional accounts. |
| **App roles / Instagram tester accounts** | Real people with Administrator / Developer / Tester role; Instagram accounts are attached under **Instagram > API setup with Instagram login** (or **App Roles > Roles**). "Multiple accounts can be added for multiple testers." | **Yes** — this is the test mechanism: add a second, throw-away professional account and publish to it. |
| **Graph API Explorer** (`developers.facebook.com/tools/explorer`) | Interactive console that issues tokens for a selected app and sends real calls. | Manual exploration only; calls act on live data. The Explorer guide does not mention selecting `graph.instagram.com`; using it with Instagram-Login tokens is **UNVERIFIED**. For the Instagram-Login path the dashboard's **Generate token** button plus `curl` is the documented route. |
| **API Integration Helper** | Dashboard tool that sends a test **message** (messaging API). | No. |
| **Postman** | Meta's 2023 developer blog links a public workspace: `https://www.postman.com/meta/workspace/instagram/` ("Postman Workspace for Reels Publishing APIs on Instagram"). | Reference only. Could not be inspected (Postman site not fetched); whether it has been updated for Instagram Login / v25–v26 is **UNVERIFIED**. |

Practical test target: a dedicated Instagram **professional** account (Business or Creator), set to
**private** if posts must not be seen, added to the app. Two caveats: the app-creation guide says of the
account added in the token step "This account must be public" (so a private test account may be rejected at
that step — **UNVERIFIED** in practice; if so, keep it public with zero followers), and published posts
cannot be deleted through the Instagram-Login API, so test posts must be removed by hand in the app.

**Machine-readable description.** Meta publishes no OpenAPI document for the Graph or Instagram APIs.
What exists: the markdown copy of every doc page (`…/documentation/<path>.md`, used throughout this research),
`llms.txt` indexes (`/documentation/instagram-platform/llms.txt`, `/documentation/development/llms.txt`),
and the code-generated business SDKs noted in the integration doc. None is a schema a fake can be validated
against mechanically. Fixtures in section D are the reference.

### A.3 LinkedIn

**No sandbox, no test organization.** Verified statements:

- "Tips to Get Started" (updated 2026-05-15), "Test Artifacts": "You may create up to five test LinkedIn
  Member profiles for purposes of testing your Application only. You must create the profiles manually …
  Test profiles must: (a) generally appear to be standard LinkedIn Member profiles; (b) not interact with
  non-test profiles (for example, you may only share Content with other test profiles …); (c) not use a
  stock photo …; and (d) not override our API call limits. You must clearly identify the profiles as test
  profiles using your company's name (if applicable) in the headline 'Test Profile at [insert real company
  name]'. You may not: (1) use a test profile to manage your Application; … or (3) **create any test
  companies**." The API Terms of Use carry the same clause.
- The only "test" object LinkedIn creates through an API is a **test ad account** (Advertising API) — irrelevant.
- Community Management API **Development tier** is the nearest thing to a test tier but it calls production:
  500 calls/app/24 h, 100 calls/member/24 h, `BATCH_GET` not allowed, webhooks disabled, integration and
  testing to be completed within 12 months.
- No dry-run or validate-only parameter on `/rest/posts` or the media `initializeUpload` actions.

Practical options, in order:

1. **Member posting:** a test profile (rules above) authorised against the app; post with
   `"visibility": "CONNECTIONS"` (documented value) and delete with `DELETE /rest/posts/{urn}` (idempotent, 204).
   With no connections the post has no audience.
2. **Page posting:** there is no legitimate test Page. Two partial mitigations: (a) a real Page owned by the
   operator with few followers, delete after the run; (b) a **dark post** — the Posts API documents
   `author` = organization, `visibility: PUBLIC`, `distribution.feedDistribution: NONE` as content that
   "doesn't appear as organic content on a LinkedIn company page". The page also says dark posts need
   "details of the `adContext` type" and are meant for ad campaigns, so whether a Community Management app
   without the Advertising API may create one is **UNVERIFIED**.

**Tools LinkedIn does provide.**

- **Token Generator** (`https://www.linkedin.com/developers/tools/oauth/token-generator`): select app, OAuth
  flow and scopes, consent, copy the token. Removes the need for a redirect listener during setup.
- **Token Inspector** (`…/tools/oauth/token-inspector`) and `POST /oauth/v2/introspectToken`.
- **Postman workspaces** (official): index `https://www.postman.com/linkedin-developer-apis?tab=workspaces`;
  marketing/community workspace
  `https://www.postman.com/linkedin-developer-apis/workspace/linkedin-marketing-solutions-versioned-apis/overview`.
  Collections ship with OAuth 2.0 settings pre-filled; requires adding
  `https://oauth.pstmn.io/v1/callback` and `https://oauth.pstmn.io/v1/browser-callback` as redirect URLs.
  Collection contents were not inspected (**UNVERIFIED** that a Posts/Images collection exists at 202609).
- Developer Portal → app → **Analytics** (quota use) and the "API endpoint catalog" (which endpoints the app's
  products can reach — the authoritative answer to open question 1 in section B.3 once an app exists).

**Machine-readable description.** None. No OpenAPI; Rest.li schemas are not published. The Postman
collections are the only structured artefact and are examples, not a schema.

### A.4 Object storage (for the Instagram stager)

- Unit level: the two AWS-published S3 signature examples plus a presigned-URL example (section C.2) are
  deterministic vectors.
- Integration level: a MinIO container in the Compose test profile gives a real S3-compatible endpoint
  with no cloud credentials. (MinIO server image/version not researched here — out of scope; **UNVERIFIED**
  which tag to pin.)
- `moto` 5.2.3 (2026-08-22, Apache-2.0) can mock S3 but pulls boto3, botocore, cryptography, requests,
  werkzeug, responses, xmltodict — not worth it for two calls.

### A.5 Mock libraries versus hand-written fakes (httpx2)

| Option | Version / date / licence | Works with httpx2? | Verdict |
| --- | --- | --- | --- |
| `httpx2.MockTransport` (built in) | httpx2 2.13.1, 2026-09-23, BSD-3-Clause | Yes (confirmed locally in `interface-and-stack.md`) | **Use this.** |
| `respx` | 0.23.1, 2026-04-08, BSD-3-Clause; requires `httpx>=0.25.0` | No (targets legacy `httpx`) | No |
| `pytest-httpx` | 0.36.2, 2026-04-09, MIT; requires `httpx==0.28.*` | No | No |
| `pytest-httpx2` | 1.0.0, 2026-05-20, BSD-3-Clause; author Jonas Lundberg (the respx author), repo `lundberg/pytest-httpx2`; summary "A pytest plugin for mocking out httpx2 using respx"; **depends on `respx`, hence on legacy `httpx`** | Claimed; not exercised here (**UNVERIFIED**) | No — one release, and it re-introduces the legacy `httpx` dependency the project chose to avoid. Provenance question from `interface-and-stack.md` is answered (it is the respx author), the recommendation is unchanged. |
| Vendor-specific mocks (ptbtest, telemulator3, tgtest, moto) | see above | n/a | No |

**Recommendation: hand-written fakes at the HTTP transport layer.** Each adapter talks to 5–10 endpoints.
A fake per vendor is a `httpx2.MockTransport` handler (a function `Request -> Response`) that:

- asserts method, path, auth placement (Telegram token in path; Meta `Authorization: Bearer`; LinkedIn
  `Authorization` + `Linkedin-Version` + `X-Restli-Protocol-Version`) and body shape;
- holds a little state so multi-step flows are real (Instagram container → status → publish; LinkedIn
  initializeUpload → PUT → post);
- can be scripted to return the documented error bodies (section D.9, the Telegram 429 body, LinkedIn 401/426/429);
- records calls so tests can assert "publish was not retried".

Keep the fixtures (literal JSON from vendor docs) in `tests/fixtures/<vendor>/` with the source URL in a
comment or sidecar, and add the Telegram spec-conformance test from A.1. For Meta and LinkedIn the
equivalent conformance check is a small, manually run "live smoke" suite (pytest marker, skipped unless
credentials are present) against the safe targets named above.

---

## B. Credential onboarding ("the gateway")

### B.1 Telegram

Needs: one Telegram account, one channel. No review, no cost.

1. In Telegram open a chat with **@BotFather**, send `/newbot`.
2. Give a display **name**, then a **username**: 5–32 characters, Latin letters, digits, underscores, must
   end in `bot`; "the username cannot be changed later".
3. BotFather replies with the token, "a string, like `110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw`".
   Store it as a secret. `/token` regenerates it if lost or leaked.
4. Open the channel → channel info → **Administrators** → **Add Admin** → search the bot's username → grant
   only **Post Messages** (`can_post_messages`). A bot cannot add itself. (Client menu labels are from the
   apps, not from Telegram docs: **UNVERIFIED** wording.)
5. Get the numeric channel id. Reliable API-only method once the bot is admin:
   - Public channel: `GET https://api.telegram.org/bot<token>/getChat?chat_id=@channelusername` →
     `result.id` (form `-100…`).
   - Private channel: make it public temporarily and do the same, **or** post any message in the channel
     and call `getUpdates` once: the `channel_post.chat.id` (or `my_chat_member.chat.id` from the moment the
     bot was promoted) is the id. Updates are retained 24 h and `getUpdates` must not be used while a webhook
     is set.
6. Verify: `getMe` (token), `getChat` (id resolves), `getChatMember(chat_id, <bot id>)` → `status ==
   "administrator"` and `can_post_messages == true`.
7. Test environment: repeat steps 1–6 while logged in on the test servers and set the service's
   test-environment flag (A.1).

Values for the service: `bot token`, `chat_id` (64-bit integer or string).

### B.2 Instagram (Instagram API with Instagram Login, operator-owned account)

Prerequisites: a Facebook account registered as a Meta developer; an Instagram **professional** account
(Business or Creator) whose login the operator holds. No Facebook Page, no App Review, no Business
Verification for this use (see integration doc).

Meta currently documents **two** creation flows; both end in the same dashboard section. The dashboard is
behind a login and was not opened, so labels are quoted from the docs.

1. Go to `https://developers.facebook.com/apps/creation/` (or App Dashboard → **Create App**).
2. **App details**: app name and contact email → **Next**.
3. **Use cases**:
   - Current flow ("Create an App" + "Instagram Use Case" pages): select the use case
     **Manage messaging and content on Instagram**. "Use cases cannot be removed after you create your app."
   - Older flow still printed in "Create a Meta App for Instagram Platform": select **Other** → **Next** →
     app type **Business** → **Next**, then on the dashboard find the **Instagram** product → **Set up**.
     The get-started page still warns that a non-Business app type cannot add Instagram.
   Which of the two the dashboard offers today depends on the account: **UNVERIFIED**; follow whichever is shown.
4. **Business**: choose "I don't want to connect a business portfolio yet." (allowed; a portfolio is only
   needed for data you do not own) → **Next** → **Next** → **Go to dashboard**.
5. In the dashboard open the Instagram use case (**Customize …**) and select **API setup with Instagram
   Login** in the left menu. "You can only add one setup per app." The page shows the **Instagram app ID**
   and **Instagram app secret** (needed only for an OAuth flow or `ig_exchange_token`; record them anyway).
6. Permissions: click **Add all required permissions** — this adds `instagram_business_basic` and
   `instagram_business_manage_messages` by default. Then under **Permissions and features** make sure
   **`instagram_business_content_publish`** is added. Minimum scopes for this service:
   `instagram_business_basic`, `instagram_business_content_publish`.
7. **Generate access tokens** section → **Add account** → **Continue** → log in to the Instagram
   professional account in the pop-up → **Save** → **Got it**. The older guide adds: "This account must be
   public. Multiple accounts can be added for multiple testers." Accounts can later be managed under
   **App Roles > Roles** or **Instagram > API Setup with Instagram login**.
8. Click **Generate token** next to the account, log in to Instagram, copy the token. "Access tokens from
   the App Dashboard are long-lived and are valid for 60 days."
9. Webhooks, "Set up Instagram business login" (redirect URL) and "Complete app review" in the same panel
   are **not** required for an operator-owned account. Leave the app in Development mode (see A.2 caveat).
10. Find the Instagram user id:

    ```
    GET https://graph.instagram.com/v26.0/me?fields=user_id,username,account_type&access_token=<token>
    ```

    Use `user_id` (the "Instagram professional account ID, `<IG_ID>`") in publishing paths, not `id`
    (the app-scoped id). Response shape: see D.1 — note the `data` array in Meta's sample.
11. Give the service: `access token`, `IG user id`, optionally `Instagram app id/secret`, Graph version.
    The service refreshes the token itself (D.7); if it ever lapses for 60 days, repeat step 8.

Role requirement: the person doing steps 7–8 must hold a role on the app (Administrator/Developer/Tester);
the creator is Administrator.

### B.3 LinkedIn

Prerequisites: a LinkedIn member account; a LinkedIn **Page** to associate the app with (the Marketing
quick start lists "Create a LinkedIn Page" as step 1).

**Member posting (self-serve)**

1. `https://www.linkedin.com/developers/apps/new` → create the app. The form sits behind login and was not
   opened; the commonly required fields (app name, LinkedIn Page, logo, legal agreement) are **UNVERIFIED**.
   A Page super admin may have to verify the association (documented for the Community Management API).
2. **Products** tab → add **Share on LinkedIn** (grants `w_member_social`) and **Sign In with LinkedIn
   using OpenID Connect** (grants `openid`, `profile`, `email`). Both are "available to all developers, and
   may be added via self-service".
3. **Auth** tab → note **Client ID** and **Client Secret**; under **OAuth 2.0 settings** add a redirect URL.
   Rules: absolute, HTTPS ("Add the redirect (callback) URL via `HTTPS` to your server"), no `#`, query
   parameters ignored. Whether `http://localhost` is accepted is not stated: **UNVERIFIED**. Any HTTPS URL
   the operator controls works for a manual flow, even one that serves nothing: the code is read from the
   browser address bar.
4. Easiest first token: the **Token Generator** (A.3). Or run the flow by hand:

   ```
   https://www.linkedin.com/oauth/v2/authorization?response_type=code
     &client_id=<CLIENT_ID>
     &redirect_uri=<URL-ENCODED REDIRECT URL>
     &state=<random>
     &scope=openid%20profile%20w_member_social
   ```

   (`scope` is URL-encoded and space-delimited; add `email` only if wanted.) Log in, click **Allow**; the
   browser lands on `<redirect>?code=<CODE>&state=<same>`. Check `state`. The code lives 30 minutes.
5. Exchange it:

   ```
   curl -X POST https://www.linkedin.com/oauth/v2/accessToken \
     -H 'Content-Type: application/x-www-form-urlencoded' \
     --data-urlencode grant_type=authorization_code \
     --data-urlencode code=<CODE> \
     --data-urlencode client_id=<CLIENT_ID> \
     --data-urlencode client_secret=<CLIENT_SECRET> \
     --data-urlencode redirect_uri=<REDIRECT URL>
   ```

   Documented response (fields `refresh_token`, `refresh_token_expires_in` only "if your application is
   authorized for programmatic refresh tokens"; an `id_token` JWT is also returned with `openid`):

   ```json
   { "access_token": "AQUvlL_DYEzvT2wz1QJiEPeLioeA", "expires_in": 5184000, "scope": "r_basicprofile" }
   ```

6. Person URN: `GET https://api.linkedin.com/v2/userinfo` with `Authorization: Bearer <token>`:

   ```json
   { "sub": "782bbtaQ", "name": "John Doe", "given_name": "John", "family_name": "Doe",
     "picture": "https://media.licdn-ei.com/dms/image/…", "locale": "en-US",
     "email": "doe@email.com", "email_verified": true }
   ```

   Author = `urn:li:person:<sub>`. (`email*` are optional and may be absent.)
7. Give the service: client id, client secret, access token, expiry, refresh token if one was returned,
   person URN, `Linkedin-Version`.

**Page posting (Community Management API)** — additional steps:

8. Create a **separate new app** with no other products and request **Community Management API**
   (Development tier) on its Products tab; complete the access form (legal organisation, business email,
   Page super-admin verification — see integration doc).
9. Authorise with scope `w_organization_social r_organization_social rw_organization_admin`
   (add `w_member_social` if the same app should also post as the member; the product grants it).
10. Organization URN: `GET https://api.linkedin.com/rest/organizationAcls?q=roleAssignee&role=ADMINISTRATOR&state=APPROVED`
    with the versioned headers → `urn:li:organization:<id>` in each element. Page roles cannot be granted
    through the API, only in the LinkedIn UI.

**Open question 1 — can a self-serve "Share on LinkedIn" app call `/rest/posts` and `/rest/images`?**

Not settled by a single explicit sentence, but the primary sources lean clearly to **yes**:

- Images API page (updated 2026-06-24): "The permissions required for rest/images access are: `rw_ads`,
  `w_member_social`, `w_organization_social`, and `w_power_creators`. However, `w_member_social` permission
  are write-only and **tokens with only `w_member_social` permissions** would be unable to perform a GET
  call for rest/images." — "This only applies to versioned gateway calls." A token holding *only*
  `w_member_social` is exactly what a self-serve app produces, and the page treats its use on the versioned
  gateway as normal (write allowed, read refused with 403).
- Posts API page (updated 2026-05-13) lists `w_member_social` — "Post, comment, and like posts on behalf of
  an authenticated member" — with no product condition, and its 403 text names the scope, not a product.
- Against: the only self-serve guide ("Share on LinkedIn", content dated 2021, page updated 2023-12-14)
  documents only `POST /v2/ugcPosts` and `/v2/assets?action=registerUpload`; the Marketing permissions
  table lists the products granting `w_member_social` as "Advertising API, Community Management API"
  (it is a Marketing-docs table and omits consumer products).
- A third-party vendor blog found by search states that `/rest/posts` accepts self-serve `w_member_social`
  tokens (non-primary; not opened in full).

Verdict: **probably yes; UNVERIFIED until one call is made with a real self-serve token.** Implement the
versioned Posts/Images path as the only member path, do not poll `GET /rest/images` for member tokens, and
make the first live smoke test `POST /rest/posts` (text only, `CONNECTIONS`). If it returns 403
`ACCESS_DENIED`, the fallback is the legacy `ugcPosts` request body — keep that as a documented contingency,
not as shipped code. The Developer Portal's endpoint catalog for the app will show the answer before any call.

**Open question 2 — do non-partner apps receive refresh tokens?**

**No, per the documentation.** "LinkedIn supports programmatic refresh tokens for all approved Marketing
Developer Platform (MDP) partners" (refresh-tokens page, updated 2025-10-08); "Programmatic refresh tokens
are available for a limited set of partners. If this feature has been enabled for your application…"
(authorization-code-flow page, updated 2026-05-15). Neither self-serve page mentions refresh tokens, and the
documented sample token response has none. A self-serve Share on LinkedIn app should therefore expect
`access_token` + `expires_in` (5184000) only, and a manual re-authorisation about every 60 days.
Whether a Community Management API **Development-tier** app counts as an "approved MDP partner" for this
purpose remains **UNVERIFIED**. Code must branch on the presence of `refresh_token`.

---

## C. S3-compatible staging without a heavy SDK

Requirement: `PUT` one object, `DELETE` one object, on AWS S3 / Cloudflare R2 / MinIO / Backblaze B2, from an
async httpx2 service; optionally produce a presigned GET URL.

### C.1 Options (PyPI metadata read today; sizes measured with `pip install --target` on Python 3.14)

| Option | Version / date | Licence | Runtime deps pulled | Installed size | Async / httpx2 | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| **Hand-written SigV4 over httpx2** | — | — | none (stdlib `hmac`, `hashlib`, `urllib.parse`) | ~40 lines | native | **Recommended** |
| `boto3` | 1.43.111, 2026-10-09 | Apache-2.0 | botocore, s3transfer, jmespath, python-dateutil, six, urllib3 (7 distributions) | **28 MB** | sync only; own HTTP stack (urllib3) | No |
| `botocore` alone (use `botocore.auth.S3SigV4Auth` as signer) | 1.43.111, 2026-10-09 (16.0 MB wheel) | Apache-2.0 | jmespath, python-dateutil, six, urllib3 | ~27 MB | signer is HTTP-agnostic, usable with httpx2 by copying headers | No — 27 MB and a release **every weekday** for 40 lines of HMAC |
| `aiobotocore` | 3.9.2, 2026-10-01 | Apache-2.0 | aiohttp (+ aiosignal, frozenlist, multidict, yarl, propcache, attrs, aiohappyeyeballs), aioitertools, wrapt, botocore (pinned `<1.43.107`), … (17 distributions) | **31 MB** | async, but on **aiohttp**, not httpx2 | No — second HTTP client stack |
| `minio` | 7.2.20, 2025-11-27 | Apache-2.0 | urllib3, certifi, pycryptodome, argon2-cffi (+ bindings, cffi, pycparser), typing-extensions (9 distributions, native wheels) | 8 MB | sync only (urllib3) | No |
| `miniopy-async` | 1.23.5, 2026-03-31 | Apache-2.0 | aiohttp, aiohttp-retry, pycryptodome, argon2-cffi, certifi (`<2026`) | not measured | async on aiohttp | No |
| `httpx-auth` (has an AWS4 auth class) | 0.23.1, 2025-01-07 | MIT | **`httpx==0.28.*`** | small | legacy httpx only | No |
| `httpx-aws-auth` | 4.2.0, 2026-05-22 | not declared in metadata | `httpx>=0.28.0` | 5 kB wheel | legacy httpx only | No |
| `requests-aws4auth` | 1.4.0, 2026-10-09 | MIT | `requests` | small | requests only | No |
| `aws-requests-auth` | 0.4.3, **2020-05-27** | not declared | `requests` | small | requests only; unmaintained | No |
| `aws-request-signer` | 1.2.0, **2023-02-20** | MIT | none | 7.5 kB wheel | framework-agnostic | No — stale; no advantage over own code |
| `aws-sigv4` | 0.0.1, 2025-04-07 | not declared | none | 4 kB | — | No — single 0.0.1 release |
| `obstore` | 0.11.1, 2026-08-21 | not in metadata | none (Rust extension, 5.8 MB wheel) | not measured | async, own HTTP stack | No — overkill, native binary |

No maintained, dependency-free signer that targets httpx2 exists on PyPI today.

### C.2 Hand-written SigV4: size and how to test it

Local check: a signer supporting header-signed `PUT`/`DELETE` and query-signed (presigned) `GET` was written
in the scratch directory: **41 non-blank lines**, stdlib only (three helpers plus `sign_headers()` and
`presign_get()`). Results:

- Reproduces the three published S3 vectors (access key `AKIAIOSFODNN7EXAMPLE`, secret
  `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`, bucket `examplebucket`, region `us-east-1`,
  time `20130524T000000Z`):
  - GET `/test.txt` with `Range: bytes=0-9` → `f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41`
  - PUT `/test%24file.text`, body `Welcome to Amazon S3.`, headers `Date`, `x-amz-storage-class:
    REDUCED_REDUNDANCY` → `98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd`
  - Presigned GET `/test.txt`, `X-Amz-Expires=86400` → `aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404`

  These vectors are from the S3 API reference pages "Authenticating Requests: Using the Authorization Header"
  and "…Using Query Parameters". **Caveat:** those two pages are JavaScript-rendered and could not be read
  by the fetch tools today (HTTP 200, empty body); the expected values were supplied from prior knowledge
  and are corroborated by the next bullet, but re-read them in a browser before pinning them as fixtures.
- Byte-identical signatures to `botocore.auth.S3SigV4Auth` 1.43.111 for a path-style `PUT` (R2-style host,
  region `auto`, key containing an encoded space, extra `content-type` header) and a path-style `DELETE`
  against an `http://minio:9000` host.

What the implementation must get right (all visible in the 41 lines):

- Canonical request = method, URI-encoded path (S3: encode once, keep `/`), sorted query, lower-cased sorted
  headers each `name:value\n`, signed-header list, payload hash.
- S3 requires `x-amz-content-sha256`; use the hex SHA-256 of the body, or the literal `UNSIGNED-PAYLOAD`
  (over HTTPS) to avoid hashing a large file twice. `x-amz-date` and `host` are always signed.
- Signing key: HMAC chain `"AWS4"+secret → date → region → "s3" → "aws4_request"`.
- Region: real region for AWS; `auto` for R2 (Cloudflare: "Required by SDK but not used by R2"); any
  configured value for MinIO (default `us-east-1`, **UNVERIFIED** here); B2 region is in the endpoint name
  (**UNVERIFIED**, B2 docs not opened).
- Addressing: use **path-style** (`https://<endpoint>/<bucket>/<key>`) so one code path serves R2, MinIO, B2
  and AWS. AWS's long-term stance on path-style for new buckets was not re-checked: **UNVERIFIED**; make
  addressing style a config value.
- Requests must reach the service within five minutes of `x-amz-date` (clock sync matters in the container).
- Do not sign headers a proxy may rewrite; sign only `host`, `x-amz-*`, `content-type`.

Test material:

- Spec: `https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html` (opened) and its children
  `reference_sigv-signing-elements`, `reference_sigv-create-signed-request` (HTTP 200).
- S3 specifics and the vectors above: `https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html`,
  `https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html`.
- Official generic test suite: the old download
  (`docs.aws.amazon.com/general/latest/gr/signature-v4-test-suite.html` and its zip) is **gone** (redirect /
  404 today). The maintained copy is in AWS's own C library:
  `https://github.com/awslabs/aws-c-auth/tree/main/tests/aws-signing-test-suite/v4` (38 cases, Apache-2.0,
  repo pushed 2026-10-09). Those cases use service `service`, not `s3`, and exercise header and path
  normalisation an S3-only signer deliberately does not implement (S3 does not normalise paths), so the S3
  doc vectors plus a MinIO round-trip are the better acceptance test; the suite is optional.
- Dev-only cross-check against botocore is possible but would add botocore to the dev dependencies; the
  three vectors make it unnecessary.

**Recommendation: hand-written SigV4 (about 40 lines) in the stager module, tested with the three AWS
vectors and a MinIO integration test.** It adds no dependency, stays on the single httpx2 client (same
timeouts, pooling, MockTransport), and the algorithm has been stable since 2012. Not implemented and not
needed: multipart upload (single PUT is limited to 5 GB on S3 — **UNVERIFIED** today, far above Instagram's
300 MB), chunked `aws-chunked` signing, SigV4A, STS session tokens (add `x-amz-security-token` to the signed
headers if ever required).

### C.3 Presigned GET versus public-read objects for Meta's fetcher

Facts:

- AWS: SigV4 presigned URLs are valid "as high as 7 days" with IAM-user keys; with temporary credentials the
  URL dies with the credential. "Amazon S3 checks the expiration date and time of a signed URL at the time
  of the HTTP request" — a download already in progress continues.
- R2: presigned GET/HEAD/PUT/DELETE, "from 1 second to 7 days (604,800 seconds)"; "Presigned URLs work with
  the S3 API domain (`<ACCOUNT_ID>.r2.cloudflarestorage.com`) and cannot be used with custom domains."
  Public access is via a **custom domain** or the `r2.dev` subdomain, which is "rate-limited and should only
  be used for development purposes". Public buckets do not expose a listing.
- Meta: media "must be hosted on a publicly accessible server at the time of the attempt"; URLs should be
  US-ASCII only. Whether Meta's fetcher accepts long query strings / follows redirects is still
  **UNVERIFIED** (carried over from the integration doc).

Recommendation:

1. **Default: presigned GET URL, private bucket.** Nothing is ever publicly listable or guessable, no bucket
   policy has to be opened, it works identically on AWS, R2, MinIO and B2, and expiry is a second line of
   defence if the delete fails. A SigV4 presigned URL is plain ASCII (hex signature, percent-encoded
   credential scope).
2. **Expiry: 1 hour by default, configurable, hard-capped at 24 hours.** Reasoning: image containers are
   fetched within seconds; Meta advises polling video containers "once per minute, for no more than
   5 minutes"; a container itself expires after 24 hours, so a longer URL has no use. One hour covers a slow
   300 MB reel fetch with retries. The stager still deletes the object as soon as the post reaches a
   terminal state.
3. **Fallback mode: public base URL** (`public_base_url` config) for operators who front the bucket with a
   CDN or R2 custom domain, or if the first live test shows Meta rejecting presigned URLs. In that mode use
   unguessable keys (≥128 bits of randomness in the key), no listing, and rely on delete + a bucket lifecycle
   rule (e.g. expire after 1 day) for cleanup.

Do not use object ACLs (`x-amz-acl: public-read`): AWS disables ACLs on new buckets by default and R2 has
no ACLs (both **UNVERIFIED** today — not re-read), so it is the least portable option.

---

## D. Instagram implementation reference (literal shapes)

Source: the `.md` copies of the content-publishing guide and endpoint references, read in full today.
Host for this service: `https://graph.instagram.com/<version>`. Meta's examples print `v25.0`; some reference
pages print `graph.facebook.com` even for endpoints valid on both hosts. JSON labelled "illustrative" is
assembled from documented fields because Meta gives no literal sample.

Request encoding: the guide's examples send `Content-Type: application/json` with
`Authorization: Bearer <token>`; the references use query parameters with `access_token`. Both are
documented. Use JSON bodies + bearer header (keeps the token out of URLs and logs).

### D.1 Identify the account

```
GET /v26.0/me?fields=user_id,username,account_type
```

Meta's sample response (get-started page, literal apart from the missing comma in their sample):

```json
{ "data": [ { "user_id": "<IG_ID>", "username": "<IG_USERNAME>" } ] }
```

**Differs from `docs/INTEGRATIONS/instagram.md`**, which implies a flat object. The `/me` reference says it
"translates to `GET /{user-id}`", which for every other Graph node returns a flat object
(`{"user_id": "…", "username": "…", "id": "…"}`). The two shapes are both plausible; **which one production
returns is UNVERIFIED**. The client must accept both: if the body has a `data` list, take `data[0]`.

### D.2 Image container

```
POST /v26.0/<IG_ID>/media
Content-Type: application/json
Authorization: Bearer <ACCESS_TOKEN>

{ "image_url": "https://www.example.com/images/bronz-fonz.jpg", "caption": "…", "alt_text": "…" }
```

```json
{ "id": "<IG_CONTAINER_ID>" }
```

Reference sample response: `{ "id": "17889455560051444" }`. Optional: `caption`, `alt_text`, `location_id`,
`user_tags`, `is_ai_generated`. No `media_type` for a feed image.

### D.3 Reel (video) container

```
POST /v26.0/<IG_ID>/media

{ "media_type": "REELS", "video_url": "https://www.example.com/videos/bronz-fonz.mp4",
  "caption": "…", "share_to_feed": true, "cover_url": "https://…/cover.jpg", "thumb_offset": 0 }
```

```json
{ "id": "<IG_CONTAINER_ID>" }
```

Guide: "`media_type` — If the container will be for a video, set to `VIDEO`, `REELS`, or `STORIES`"; the
`/media` parameter table lists only `CAROUSEL`, `REELS`, `STORIES` as values and uses `VIDEO` only for a
carousel video child. A stand-alone feed video is a reel. Only resumable sessions (Facebook Login) add a
`uri` field to the response.

### D.4 Carousel

Children (2–10; images and/or videos; no reels):

```
POST /v26.0/<IG_ID>/media
{ "image_url": "https://…/1.jpg", "is_carousel_item": true, "alt_text": "…" }

POST /v26.0/<IG_ID>/media
{ "media_type": "VIDEO", "video_url": "https://…/2.mp4", "is_carousel_item": true }
```

each → `{ "id": "<IG_CONTAINER_ID_n>" }`. No `caption` on children.

Parent (Meta's guide example, with the commas their sample omits):

```
POST /v26.0/<IG_ID>/media
{ "media_type": "CAROUSEL", "caption": "Fruit candies",
  "children": "<IG_CONTAINER_ID_1>,<IG_CONTAINER_ID_2>,<IG_CONTAINER_ID_3>" }
```

```json
{ "id": "<IG_CAROUSEL_CONTAINER_ID>" }
```

Note for implementation: the guide sends `children` as **one comma-separated string**, while the reference
describes it as "An array of up to 10 container IDs". Send the comma-separated string (the only literal
example); whether a JSON array is also accepted is **UNVERIFIED**. Meta's own sample shows the caption
URL-encoded inside JSON (`"Fruit%20candies"`), which is a documentation slip — send plain text in JSON.
`is_ai_generated` goes on the parent only.

### D.5 Status poll

```
GET /v26.0/<IG_CONTAINER_ID>?fields=status_code,status
```

Meta's sample (requesting only `status_code`):

```json
{ "status_code": "FINISHED", "id": "17889615691921648" }
```

`status_code` ∈ `EXPIRED | ERROR | FINISHED | IN_PROGRESS | PUBLISHED`. `status`: "Publishing status. If
`status_code` is `ERROR`, this value will be an error subcode." No literal sample of an error status is
published; illustrative:

```json
{ "status_code": "ERROR", "status": "2207026", "id": "17889615691921648" }
```

The exact form of `status` (bare subcode, or text containing it) is **UNVERIFIED** — parse with a regex for
a 7-digit `22070\d\d` number and keep the raw string. Cadence: "once per minute, for no more than 5 minutes".
Other field: `copyright_check_status { status, matches_found }`.

### D.6 Publish, then permalink

```
POST /v26.0/<IG_ID>/media_publish
{ "creation_id": "<IG_CONTAINER_ID>" }
```

```json
{ "id": "17920238422030506" }
```

```
GET /v26.0/<IG_MEDIA_ID>?fields=id,permalink,media_type,timestamp
```

`permalink` ("Permanent URL to the media") and `shortcode` are documented fields; "Some fields, such as
`permalink`, cannot be used on photos within albums (children)". Meta's literal sample for this endpoint
requests other fields:

```json
{ "id": "17918920912340654", "media_type": "IMAGE", "media_url": "https://sconten...",
  "owner": { "id": "17841405309211844" }, "timestamp": "2019-09-26T22:36:43+0000" }
```

Illustrative for our call (permalink format not shown in Meta's docs — **UNVERIFIED**, treat as opaque):

```json
{ "id": "17918920912340654", "permalink": "https://www.instagram.com/p/<shortcode>/",
  "media_type": "IMAGE", "timestamp": "2026-10-09T12:00:00+0000" }
```

Timestamp format is `+0000` without a colon — not parseable by a strict RFC 3339 parser; use
`%Y-%m-%dT%H:%M:%S%z`. A reel reads back `media_type: "VIDEO"`, a carousel `CAROUSEL_ALBUM`
(value from the media reference field list; not re-quoted today — **UNVERIFIED**).

### D.7 Publishing limit

```
GET /v26.0/<IG_ID>/content_publishing_limit?fields=quota_usage,config
```

```json
{ "data": [ { "quota_usage": 2, "config": { "quota_total": 50, "quota_duration": 86400 } } ] }
```

Literal from the reference. `quota_usage` is the default field; optional `since` (Unix time, ≤ 24 h old).
Meta's own sample request asks for `fields=quota_usage,rate_limit_settings` while its field table and
sample response use `config` — request `config`. The 50-versus-100 contradiction stands (guide: "100
API-published posts within a 24-hour moving period"; same guide's carousel section and the reference: 50);
read `config.quota_total`.

### D.8 Token calls

Refresh (token ≥ 24 h old, unexpired, has `instagram_business_basic`):

```
GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=<LONG_LIVED_TOKEN>
```

```json
{ "access_token": "c3oxd...", "token_type": "bearer", "expires_in": 5183944 }
```

Literal from the reference; no version segment in the path. "Refreshed tokens are valid for 60 days from the
date at which they are refreshed." Persist the returned `access_token`; compute expiry as now + `expires_in`.

For completeness (not needed on the dashboard-token path): the code exchange at
`POST https://api.instagram.com/oauth/access_token` is documented as returning a **`data` array**:

```json
{ "data": [ { "access_token": "EAACEdEose0...", "user_id": "1020...",
              "permissions": "instagram_business_basic,instagram_business_content_publish" } ] }
```

and the short-to-long exchange (`GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token
&client_secret=…&access_token=…`) returns the same three fields as the refresh call.

### D.9 Error bodies

Meta's literal sample (error-codes reference), HTTP 400:

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

Illustrative fixtures for the fake, built from that shape and the documented code/subcode/message table
(the `message` and `type` values are **UNVERIFIED**; the code, subcode and user message are documented):

```json
{ "error": { "message": "…", "type": "OAuthException", "code": 9007, "error_subcode": 2207027,
  "is_transient": false, "error_user_msg": "The media is not ready for publishing, please wait for a moment",
  "fbtrace_id": "A…" } }
```

```json
{ "error": { "message": "…", "type": "OAuthException", "code": 9, "error_subcode": 2207042,
  "is_transient": false,
  "error_user_msg": "You reached maximum number of posts that is allowed to be published by Content Publishing API.",
  "fbtrace_id": "A…" } }
```

```json
{ "error": { "message": "…", "type": "OAuthException", "code": 9004, "error_subcode": 2207052,
  "is_transient": false, "error_user_msg": "The media could not be fetched from this uri: https://…",
  "fbtrace_id": "A…" } }
```

```json
{ "error": { "message": "Error validating access token: Session has expired …", "type": "OAuthException",
  "code": 190, "error_subcode": 463, "fbtrace_id": "A…" } }
```

The last one follows the generic Graph error format (no `error_user_*`, no `is_transient`); its message text
is **UNVERIFIED**. The fake must therefore tolerate absent `error_subcode`, `is_transient`,
`error_user_title`, `error_user_msg`. New in the error table since the integration doc was written: a block
of `100` / `9050–9062` errors for the `/ig_audio` endpoint (audio replacement) — not relevant to publishing.

### D.10 Differences from `docs/INTEGRATIONS/instagram.md`

1. `/me` sample response is wrapped in `{"data": [ … ]}` (D.1). Accept both shapes.
2. App creation: Meta now documents a **use case** ("Manage messaging and content on Instagram") alongside the
   older "Other → Business" flow. The integration doc's "Create a Meta app of type Business" is still
   supported by the get-started page but may not be the path the dashboard shows (B.2 step 3).
3. The dashboard adds `instagram_business_manage_messages` as a required default permission;
   `instagram_business_content_publish` must be added explicitly (B.2 step 6).
4. The account added for token generation "must be public" according to the app-creation guide (B.2 step 7).
5. `children` is a comma-separated string in the only literal example; the reference calls it an array (D.4).
6. `content_publishing_limit`: Meta's sample request uses a field name (`rate_limit_settings`) that is not in
   its field table; use `config` (D.7).
7. Everything else checked today — container parameters, status values, poll cadence, refresh call and
   response, error sample, 50/100 inconsistency, `v25.0` in examples — matches the integration doc.

---

## UNVERIFIED items (consolidated)

1. Telegram: whether test servers accept an existing phone number / how login codes arrive; client menu labels.
2. Telegram: local Bot API server `/test/` routing is source-derived, not documented.
3. Instagram: public visibility of posts published while the app is in Development mode.
4. Instagram: whether a private professional account can be added in "Generate access tokens".
5. Instagram: which app-creation flow (use case vs Other → Business) the dashboard presents today.
6. Instagram: real `/me` response shape (`data` array vs flat); `status` string form on `ERROR`; permalink
   format; `children` as JSON array; `CAROUSEL_ALBUM` read-back value; message text of non-publishing errors.
7. Instagram: Meta fetcher behaviour with presigned (long query string) URLs and redirects.
8. Meta Postman workspace currency; Graph API Explorer support for `graph.instagram.com`.
9. LinkedIn: self-serve app on `/rest/posts` / `/rest/images` (strong documentary indication, no explicit statement).
10. LinkedIn: refresh tokens for a Community Management Development-tier app.
11. LinkedIn: app-creation form fields; whether `http://localhost` redirect URLs are accepted; Token
    Generator redirect requirements; dark posts without the Advertising API; Postman collection contents.
12. S3: the three signature vectors were not re-read from the (unrenderable) AWS pages today — confirmed
    only by matching botocore; path-style support policy on AWS; ACL defaults; MinIO default region; B2 details;
    5 GB single-PUT limit.
13. `pytest-httpx2` actually working with httpx2 (not exercised); `rockneurotiko/telegram_api_json` version.

---

## Sources consulted

Telegram
- https://core.telegram.org/bots/features
- https://core.telegram.org/bots/webapps (opened; the test-environment section was not present in the retrieved text)
- https://core.telegram.org/bots/api
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/README.md
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/telegram-bot-api/telegram-bot-api.cpp
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/telegram-bot-api/ClientManager.cpp
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/telegram-bot-api/HttpConnection.cpp
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/telegram-bot-api/Client.cpp
- https://raw.githubusercontent.com/PaulSonOfLars/telegram-bot-api-spec/main/api.json
- https://api.github.com/repos/PaulSonOfLars/telegram-bot-api-spec/contents/
- https://ark0f.github.io/tg-bot-api/openapi.json
- https://api.github.com/repos/{ark0f/tg-bot-api, PaulSonOfLars/telegram-bot-api-spec, sys-001/telegram-bot-api-versions, rockneurotiko/telegram_api_json, davtur19/TuriBotGen, tdlib/telegram-bot-api, aiogram/aiogram, grom-dev/bot-api-spec}

Meta / Instagram (markdown copies)
- https://developers.facebook.com/documentation/instagram-platform/content-publishing.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media_publish.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-container.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/error-codes.md
- https://developers.facebook.com/documentation/instagram-platform/reference/instagram-media.md
- https://developers.facebook.com/documentation/instagram-platform/reference/me.md
- https://developers.facebook.com/documentation/instagram-platform/reference/refresh_access_token.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login.md (downloaded, not used)
- https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/get-started.md
- https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login.md
- https://developers.facebook.com/documentation/instagram-platform/create-an-instagram-app.md
- https://developers.facebook.com/documentation/instagram-platform/llms.txt
- https://developers.facebook.com/documentation/development/llms.txt
- https://developers.facebook.com/documentation/development/create-an-app.md
- https://developers.facebook.com/documentation/development/create-an-app/instagram-use-case.md
- https://developers.facebook.com/documentation/development/create-an-app/other-app-types/instagram-apis.md
- https://developers.facebook.com/documentation/development/create-an-app/other-app-types.md (downloaded, not used)
- https://developers.facebook.com/documentation/development/app-customization.md (downloaded, stub)
- https://developers.facebook.com/documentation/development/build-and-test.md (downloaded, stub)
- https://developers.facebook.com/documentation/development/build-and-test/app-modes.md
- https://developers.facebook.com/documentation/development/build-and-test/app-roles.md
- https://developers.facebook.com/documentation/development/build-and-test/test-apps.md
- https://developers.facebook.com/documentation/development/build-and-test/test-users.md
- https://developers.facebook.com/docs/graph-api/guides/explorer/
- https://developers.facebook.com/blog/post/2023/03/24/get-started-with-reels-publishing-apis-with-postman/

LinkedIn
- https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin
- https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2
- https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access
- https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow
- https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens
- https://learn.microsoft.com/en-us/linkedin/shared/authentication/postman-getting-started
- https://learn.microsoft.com/en-us/linkedin/shared/authentication/developer-portal-tools
- https://learn.microsoft.com/en-us/linkedin/marketing/authentication/lms-generate-an-access-token
- https://learn.microsoft.com/en-us/linkedin/marketing/quick-start
- https://learn.microsoft.com/en-us/linkedin/marketing/tips-to-get-started
- https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/images-api
- https://www.linkedin.com/legal/l/api-terms-of-use

Object storage and signing
- https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html
- https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html (HTTP status only)
- https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html (HTTP 200, body not renderable)
- https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html (HTTP 200, body not renderable)
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html
- https://docs.aws.amazon.com/general/latest/gr/signature-v4-test-suite.html (redirects to the General Reference root; page gone)
- https://docs.aws.amazon.com/general/latest/gr/samples/aws-sig-v4-test-suite.zip (404)
- https://github.com/awslabs/aws-c-auth/tree/main/tests/aws-signing-test-suite/v4 (and the GitHub API listing of that directory; `…/tests/aws-sig-v4-test-suite` is 404)
- https://api.github.com/repos/awslabs/aws-c-auth
- https://developers.cloudflare.com/r2/api/s3/presigned-urls/
- https://developers.cloudflare.com/r2/buckets/public-buckets/

Package metadata (`https://pypi.org/pypi/<name>/json`)
- boto3, botocore, aiobotocore, aioboto3, s3transfer, jmespath, minio, miniopy-async, aiohttp-s3-client,
  aws-requests-auth, requests-aws4auth, httpx-auth, httpx-aws-auth, aws-request-signer, aws-sigv4, obstore,
  s3fs, moto, pycryptodome, argon2-cffi, types-aiobotocore, botocore-a-la-carte, httpx2, pytest-httpx2,
  respx, pytest-httpx (`aws4` and `minio-py` returned no project)

Search-result pages (titles and snippets only; not opened in full, used only where marked non-primary)
- https://www.blotato.com/blog/linkedin-posting-api
- https://github.com/elebur/ptbtest , https://pypi.org/project/tgtest/ , https://pypi.org/project/telemulator3
- https://bazel.googlesource.com/bazel/+/refs/tags/1.0.1/third_party/aws-sig-v4-test-suite
- https://apis.io/providers/telegram/ , https://api.apis.guru/v2/specs/telegram.org/5.0.0.json

Local checks (scratch directory, nothing installed into the project)
- `pip install --target` of boto3, minio, aiobotocore to measure installed size and distribution count.
- A 41-line stdlib SigV4 signer verified against the three S3 documentation vectors and against
  `botocore.auth.S3SigV4Auth` 1.43.111 for path-style PUT and DELETE.
