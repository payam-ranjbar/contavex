Researched: 2026-10-09
Bot API version: 10.3 (released 2026-08-24). Local Bot API server (tdlib/telegram-bot-api) master is also at 10.3.

# Telegram Bot API integration (channel publishing)

Scope: publishing to a Telegram channel through a bot that is an administrator of that channel.

Conventions in this document:

- "Docs" = stated on core.telegram.org. "Server source" = read from the open-source Bot API server code (tdlib/telegram-bot-api), which is the same code Telegram runs in front of the cloud API; behaviour derived from it is real but not a documented contract.
- **UNVERIFIED** = could not be confirmed against a primary source on the research date. Treat as a hypothesis to be covered by an integration test.

## Authentication

- One static bearer-style secret per bot: the **bot token**, issued by @BotFather. Example shape from the docs: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` (`<numeric bot id>:<secret>`).
- The token is part of the **URL path**, not a header: `https://api.telegram.org/bot<token>/METHOD_NAME`. HTTPS is mandatory on the cloud API.
- There is no OAuth, no scopes, no expiry, no refresh. Whoever holds the token fully controls the bot ("it can be used by anyone to control your bot").
- HTTP methods: GET and POST. Parameter encodings: URL query string, `application/x-www-form-urlencoded`, `application/json` (not for file uploads), `multipart/form-data` (required for file uploads). All requests UTF-8. Method names are case-insensitive.
- Credential check: `getMe` — "A simple method for testing your bot's authentication token." No parameters; returns a `User` for the bot. An invalid token yields `401` (`Unauthorized: invalid token specified` per server source).
- Test environment: `https://api.telegram.org/bot<token>/test/METHOD_NAME`. It is a fully separate environment (separate user account and separate bot/token); flood limits there "may at times be stricter".
- Logging hazard: because the token is in the path, it leaks into access logs, proxy logs, HTTP client debug output, exception messages that include the request URL, and APM traces. The service must redact `/bot<token>/` everywhere a URL can be rendered.

## Account requirements

- A bot created with @BotFather (`/newbot`). Bot username is 5-32 characters, Latin letters/digits/underscore, must end in `bot`, and cannot be changed later.
- A Telegram channel, and a human channel owner/admin who adds the bot **as an administrator** of that channel. A bot cannot add itself.
- No app review, business verification, or paid plan is required to post to a channel.
- Optional, only for a self-hosted Bot API server: an `api_id` / `api_hash` pair obtained from Telegram (see Production requirements).
- Paid broadcasts (not needed for channel posting) require Telegram Stars on the bot balance; see Rate limits.

## Permissions

Channel administrator rights relevant to publishing (fields of `ChatAdministratorRights` / `ChatMemberAdministrator`):

| Right | Docs wording | Needed for |
|---|---|---|
| `can_post_messages` | "can post messages in the channel, approve suggested posts, or access channel statistics; for channels only" | Sending any post. Also sufficient to delete the bot's **own** posts: "Bots granted can_post_messages permissions can delete outgoing messages in channels." |
| `can_edit_messages` | "can edit messages of **other users** and can pin messages; for channels only" | Editing posts authored by someone else; pinning. |
| `can_delete_messages` | "can delete messages of other users" | Deleting posts authored by someone else ("it can delete any message there"). |
| `can_manage_chat` | "Implied by any other administrator privilege." | Nothing to configure; always present. |

Least privilege for this service: **`can_post_messages` only.**

- Post: `can_post_messages` (docs).
- Delete own posts: `can_post_messages` (docs, explicit).
- Edit own posts: the docs define `can_edit_messages` as the right to edit *other users'* messages, which implies own posts are editable with `can_post_messages` alone. This is an inference from the wording, not an explicit statement — **UNVERIFIED**; confirm with an integration test before promising edit support under least privilege.
- Grant `can_edit_messages` / `can_delete_messages` only if the service must modify posts it did not author.

Preflight checks the service can run: `getMe` (token valid, returns bot id), then `getChat(chat_id)` (channel resolvable, returns `ChatFullInfo` including the numeric id), then `getChatMember(chat_id, <bot id>)` and inspect `status == "administrator"` and `can_post_messages`.

Paid media: the features page says paid media can be posted in "channel chats where it has the relevant paid-media permission". The exact right name was not confirmed — **UNVERIFIED**; out of scope unless paid posts are required.

## Publishing workflow

Publishing is **synchronous**. Every send method returns the created object in the HTTP response; there is no job id, no polling, no publish webhook.

| Action | Method | Success result |
|---|---|---|
| Text post | `sendMessage` | `Message` (has `message_id`) |
| Single image | `sendPhoto` | `Message` |
| Single video | `sendVideo` | `Message` |
| File | `sendDocument` | `Message` |
| GIF / silent H.264 | `sendAnimation` | `Message` |
| Album | `sendMediaGroup` | `Array of Message` (one `message_id` per item, shared `media_group_id`) |
| Edit text | `editMessageText` | edited `Message` |
| Edit caption | `editMessageCaption` | edited `Message` |
| Replace media | `editMessageMedia` | edited `Message` |
| Delete | `deleteMessage` / `deleteMessages` | `True` |

Recommended sequence for one publish request:

1. Resolve credentials and target `chat_id`; optionally preflight (cache the result).
2. Validate locally against the limits below (text length, caption length, item count, file size) so permanent errors never reach Telegram.
3. One HTTPS call to the send method.
4. On `ok: true`, persist `chat.id` + `message_id` (all ids for an album) as the external post reference. The pair (`chat_id`, `message_id`) is the handle for later edit/delete.
5. On failure, classify per the Errors table.

Channel targeting (`chat_id`, "Integer or String"):

- `@channelusername` — only for public channels (those with a username).
- Numeric id — works for both public and private channels. Channel ids have the form `-100<internal id>`; the server source computes them as `-1000000000000 - supergroup_id`. Ids can exceed 32 bits (up to 52 significant bits): store as 64-bit integer or string.
- Private channels have no username, so the numeric id is mandatory. Obtain it once via `getChat` while the channel is temporarily public, from an update the bot receives (`channel_post`, `my_chat_member`), or from a client. Usernames can be changed or removed by the owner, so persist the **numeric id** as the canonical target and treat `@username` as onboarding input only.

Text (`sendMessage`):

- `text`: 1-4096 characters **after entities parsing** (markup characters do not count; the rendered text does).
- Formatting — exactly one of: `parse_mode` = `HTML` | `MarkdownV2` | `Markdown` (legacy, do not use), **or** an explicit `entities` array of `MessageEntity`.
  - HTML: only a fixed tag list (`b/strong`, `i/em`, `u/ins`, `s/strike/del`, `tg-spoiler`, `a href`, `code`, `pre`, `blockquote`, `blockquote expandable`, `tg-emoji`, `tg-time`). `<`, `>`, `&` must be escaped; only `&lt; &gt; &amp; &quot;` named entities are supported.
  - MarkdownV2: every one of ``_ * [ ] ( ) ~ ` > # + - = | { } . !`` must be backslash-escaped outside entities. Easy to get wrong with arbitrary user text.
  - Design implication: accept either pre-built `entities` or HTML from callers; HTML has the simplest, safest escaping rule. A parse failure is a permanent `400`.
- Link preview: `link_preview_options` (`LinkPreviewOptions`): `is_disabled`, `url` (defaults to first URL in the text), `prefer_small_media`, `prefer_large_media`, `show_above_text`.
- Other useful flags on all send methods: `disable_notification`, `protect_content`, `reply_markup` (inline keyboard; not available on `sendMediaGroup`).
- Custom emoji entities are restricted (bot must have bought additional usernames on Fragment, or owner Premium for non-channel chats) — do not rely on them in channels.

## Supported media

Three ways to supply a file, with different limits (docs, "Sending files"):

| Source | Photos | Other files | Notes |
|---|---|---|---|
| `file_id` (already on Telegram) | no limit | no limit | `file_id` is per-bot, cannot change type (video cannot be resent as photo), thumbnails cannot be resent. Docs: file_ids "can be treated as persistent". |
| HTTP URL (Telegram fetches it) | 5 MB | 20 MB | Target must serve the correct MIME type. For `sendDocument`, URL sending "will currently only work for .PDF and .ZIP files". Telegram's fetch failure surfaces as `400 Bad Request: failed to get HTTP URL content` / `wrong type of the web page content`. |
| `multipart/form-data` upload | 10 MB | 50 MB | Use `attach://<file_attach_name>` to reference parts from `InputMedia*` JSON. |

Per type:

- **Photo** (`sendPhoto`, `InputMediaPhoto`): at most 10 MB; `width + height` must not exceed 10000; aspect ratio at most 20. Optional `has_spoiler`, `show_caption_above_media`. Photos are recompressed; send as document to preserve the original.
- **Video** (`sendVideo`, `InputMediaVideo`): up to 50 MB on the cloud API. "Telegram clients support MPEG4 videos (other formats may be sent as Document)." Parameters: `duration`, `width`, `height`, `thumbnail` (JPEG, < 200 kB, max 320 px per side, multipart only), `cover`, `start_timestamp`, `supports_streaming` ("Pass True if the uploaded video is suitable for streaming"), `has_spoiler`, `show_caption_above_media`.
- **Document** (`sendDocument`, `InputMediaDocument`): any type, up to 50 MB. `disable_content_type_detection` (forced True inside albums). Optional `thumbnail`.
- **Animation** (`sendAnimation`): GIF or H.264/MPEG-4 AVC without sound, up to 50 MB. Not allowed inside `sendMediaGroup`.
- **Album** (`sendMediaGroup`): `media` is an array of **2-10** items of `InputMediaAudio`, `InputMediaDocument`, `InputMediaLivePhoto`, `InputMediaPhoto`, `InputMediaVideo`.
  - Mixing: photos, live photos and videos can be mixed. "Documents and audio files can be only grouped in an album with messages of the same type." Wrong mixes return `400 ... Can't use the media of the specified type in the album`.
  - Captions: there is **no album-level caption parameter**. Each item has its own `caption` (0-1024), `parse_mode`, `caption_entities`. Client convention that a caption set on only one item is displayed as the album caption is not stated in the Bot API docs — **UNVERIFIED** (set the caption on the first item only and verify visually).
  - No `reply_markup` on albums.
  - An album is N separate messages; editing/deleting is per `message_id`. `deleteMessages` (1-100 ids) removes an album in one call.
- **Paid media** (`sendPaidMedia`): up to 10 items, `star_count` 1-25000, caption 0-1024; paid posts "must not be deleted for 24 hours to receive the payment and can't be edited". Not relevant unless monetised posts are required.
- **Rich messages** (`sendRichMessage`, Bot API 10.1+): a new structured-content message type. Not evaluated for channel publishing; ignore for v1.

Captions: 0-1024 characters after entities parsing, same `parse_mode` / `caption_entities` options as text. `show_caption_above_media` is supported for animation, photo and video only.

## Limits

| Item | Limit | Source |
|---|---|---|
| Message text | 1-4096 chars after entity parsing | docs |
| Caption (photo/video/document/animation/album item) | 0-1024 chars after entity parsing | docs |
| Photo upload (multipart) | 10 MB; w+h <= 10000; ratio <= 20 | docs |
| Photo by URL | 5 MB | docs |
| Other file upload (multipart), cloud | 50 MB | docs |
| Other file by URL | 20 MB | docs |
| By `file_id` | no size limit | docs |
| Upload via local Bot API server (`--local`) | 2000 MB | docs + README |
| Thumbnail | JPEG, < 200 kB, <= 320 px per side | docs |
| Album size | 2-10 items | docs |
| `deleteMessages` batch | 1-100 ids | docs |
| Delete window | message "sent less than 48 hours ago" | docs |
| Edit window for bot's own posts | none documented (the 48 h note applies only to business messages not sent by the bot) | docs |
| File download via `getFile` | 20 MB cloud / unlimited local | docs |
| Undelivered updates retention | 24 hours | docs |

Notes:

- Whether the 10 MB photo limit still applies on a local server (versus the 2000 MB general limit) is **UNVERIFIED**.
- Text over 4096 must be split by the service into multiple posts (multiple `message_id`s) or rejected; Telegram does not split.
- A post with media and more than 1024 characters of text cannot be a single captioned message. Options: reject, or send media then a separate text message (two ids, non-atomic).

Editing rules:

- `editMessageText` (text 1-4096, `parse_mode`/`entities`, `link_preview_options`), `editMessageCaption` (0-1024, `show_caption_above_media`), `editMessageMedia` (`media`: `InputMedia`; can also replace a text message with media).
- Album items can be edited "only to an audio for audio albums, only to a document for document albums and to a photo, a live photo, or a video otherwise."
- Only messages "without reply_markup or with inline keyboards" can be edited.
- Editing to identical content returns `400 Bad Request: message is not modified ...` — treat as success for idempotent edit semantics.
- Media cannot be removed from a post and an item cannot be added to/removed from an existing album by editing; that requires delete + repost.

Deletion rules (docs, `deleteMessage`):

- "A message can only be deleted if it was sent less than 48 hours ago."
- "Bots granted can_post_messages permissions can delete outgoing messages in channels."
- With `can_delete_messages` in a channel the bot "can delete any message there". Whether that right lifts the 48 h window for channel posts is not stated — **UNVERIFIED**; design on the assumption that deletion after 48 h can fail with `400 Bad Request: message can't be deleted`.
- `deleteMessages` silently skips ids it cannot find and still returns `True`, so a `True` result does not prove each message was deleted.

## Rate limits

Documented numbers (FAQ, "My bot is hitting limits"):

| Scope | Limit |
|---|---|
| Single chat | "avoid sending more than one message per second"; short bursts may be tolerated, then `429` |
| Group | "not be able to send more than 20 messages per minute" |
| Bulk / global | "about 30 messages per second" across chats |
| Paid broadcasts | up to 1000 messages per second with `allow_paid_broadcast=true`, 0.1 Stars per message above the free 30/s |

- Channels are not named separately. Whether a channel is throttled like a "single chat" (1/s) or like a "group" (20/min) is **UNVERIFIED**. Design for the stricter figure: a per-channel token bucket of at most 20 messages/minute and 1 message/second, with `retry_after` as the authority.
- How album items count toward these limits (1 vs N messages) is **UNVERIFIED**; assume N.
- Paid broadcast eligibility is inconsistent between two official pages on the research date: the API reference says the bot needs "at least 10,000 Stars on its balance"; the FAQ says "at least 100,000 Stars on its balance and at least 100,000 monthly active users". It targets many-recipient fan-out and is irrelevant for posting to a handful of channels.
- Limits are not published as a contract: the features page says to handle errors "with retry policies" and not "depend on hardcoded limit values".

How `429` is reported:

```json
{
  "ok": false,
  "error_code": 429,
  "description": "Too Many Requests: retry after 35",
  "parameters": { "retry_after": 35 }
}
```

- `parameters.retry_after` (Integer, seconds) is the documented field: "the number of seconds left to wait before the request can be repeated".
- Server source: HTTP status is 429 and a `Retry-After` header is also set. Prefer the JSON field.
- A 429 means the request was **not** executed; retrying after the wait is safe.
- Server source: the Bot API server also applies its own per-bot back-pressure (too many concurrent queries or too much in-flight upload volume) and answers `429` with `retry_after: 60`. Keep concurrency per bot low (a small bounded worker pool; serialise per channel).

## Errors

Response envelope (docs):

- Always JSON with Boolean `ok`.
- Success: `{"ok": true, "result": ...}`.
- Failure: `{"ok": false, "error_code": <int>, "description": "<text>", "parameters": {...}?}`.
- `error_code`: "its contents are subject to change in the future". `description` is human-readable and not a stable contract either.
- `parameters` (`ResponseParameters`), optional: `retry_after` (flood control, seconds) and `migrate_to_chat_id` (a *group* was upgraded to a supergroup — does not occur for channels).
- Server source: the HTTP status code equals `error_code`.

The Bot API docs do **not** publish an error catalogue. The descriptions below are taken from the server source where marked (S); others are commonly seen strings that could not be confirmed in a primary source and are marked **UNVERIFIED**. Classify primarily on `error_code`, use `description` substring matching only as a refinement, and default unknown `400`s to permanent.

| Error | Retryable? | Action |
|---|---|---|
| `400 Bad Request: chat not found` (S) | No | Wrong `chat_id`/username, or bot never had access. Fail; mark destination misconfigured. |
| `400` parse errors (`can't parse entities ...`) (UNVERIFIED string) | No | Caller content/markup invalid. Fail with validation error. |
| `400` length errors (`Text is too long` (S); `message is too long`, `message caption is too long` UNVERIFIED strings) | No | Should be prevented by local validation. Fail. |
| `400 Bad Request: message is not modified ...` (S) | No (not an error) | Treat edit as success. |
| `400 Bad Request: message to edit not found` / `message to delete not found` (UNVERIFIED strings) | No | For delete: treat as already deleted (success). For edit: fail, post is gone. |
| `400 Bad Request: message can't be deleted` (S) / `message can't be edited` (UNVERIFIED string) | No | Outside 48 h window or insufficient right. Fail. |
| `400 Bad Request: failed to get HTTP URL content` / `wrong type of the web page content` / `wrong HTTP URL specified` (S) | Limited | Telegram could not fetch the media URL. Retry a small number of times (source may be transiently down), or fall back to downloading and uploading via multipart; then fail. |
| `400 ... Can't use the media of the specified type in the album` (S) | No | Invalid album mix. Fail. |
| `400` not-enough-rights errors (e.g. `need administrator rights in the channel chat`, `CHAT_WRITE_FORBIDDEN`) (UNVERIFIED strings) | No | Bot lacks `can_post_messages`. Fail; mark destination as needing re-authorisation. |
| `401 Unauthorized` / `Unauthorized: invalid token specified` (S) | No | Token wrong or revoked. Fail; disable the credential, stop all work for that bot, alert. |
| `403 Forbidden: bot is not a member of the channel chat` / `bot was kicked from the channel chat` (S) | No | Bot removed from channel. Fail; mark destination disconnected. |
| `404 Not Found: method not found` (S) / `404 Not Found` (S) | No | Bug: wrong method name or malformed URL. Fail; alert. |
| `409 Conflict: ...` (S) | n/a | Only arises for `getUpdates`/webhook contention ("terminated by other getUpdates request", "can't use getUpdates method while webhook is active"). A send-only service never sees it; if it polls updates, run exactly one poller per token. |
| `413` request entity too large | No | **UNVERIFIED** as a Bot API response; expected from the fronting proxy on oversized uploads. Fail; enforce size locally. |
| `429 Too Many Requests: retry after N` (S, docs) | Yes | Request not executed. Sleep `parameters.retry_after` (+ jitter), then retry. Pause the whole per-bot/per-chat queue, not just this job. |
| `500 Internal Server Error: ...` (S) | Yes, with duplicate risk | Backoff and retry. Outcome is unknown for send methods (see Known restrictions). One specific case (S): `sent message was immediately deleted and can't be returned`. |
| `502` / `503` / `504` | Yes, with duplicate risk | **UNVERIFIED** in docs (gateway-level). Backoff and retry; outcome unknown for sends. |
| Network error / timeout **before** the request was fully written | Yes | Safe to retry. |
| Network error / timeout **after** the request was sent (no response) | Ambiguous | Outcome unknown. Do not blindly retry a send; see Known restrictions. Edit and delete are safe to retry. |

Retry policy summary:

- Safe to retry freely: `getMe`, `getChat`, `getChatMember`, all `editMessage*` (same content converges; second attempt may return "not modified"), `deleteMessage(s)` (second attempt returns not-found or `True`).
- Safe to retry only when the request provably was not executed: all `send*` methods — i.e. on `429`, and on connect/TLS/DNS failures.
- Ambiguous for `send*`: read timeouts, connection resets after write, `5xx`.
- Server source note: the Go library go-telegram/bot v1.27.0 shipped a fix because "a request can be retried by HTTP/2 after the server sends GOAWAY" — transparent HTTP-client-level retries exist. Disable automatic retries of non-idempotent POSTs in whatever HTTP client is chosen, or make the decision explicitly.

## Token lifecycle

- Issued once by @BotFather at `/newbot`. Does not expire.
- Rotation/revocation: @BotFather `/token` ("If your existing token is compromised or you lost it for some reason, use the /token command to generate a new one"). Only the bot owner's Telegram account can do this, interactively; there is no rotation API for ordinary bots (`replaceManagedBotToken` exists only for "managed bots" created by another bot).
- That generating a new token immediately invalidates the old one is the long-standing behaviour but is not stated in the pages consulted — **UNVERIFIED**. Plan rotation as: generate new token → update secret store → reload service → verify with `getMe`; expect a brief window of `401`s.
- Bot ownership can be transferred in @BotFather; the new owner can then rotate the token.
- The numeric prefix of the token is the bot's user id (matches `getMe().id` and the example tokens in the docs); it is not secret, the part after `:` is.
- Storage: secret manager or mounted secret file/env var, never in images or config committed to git. Redact in logs (see Authentication). One token per bot; if several tenants/brands publish, key credentials by bot.
- Detecting revocation: any `401` from any method. There is no token-introspection endpoint beyond `getMe`.

## Production requirements

- Outbound HTTPS to `api.telegram.org:443`. No inbound connectivity is needed for a publish-only service (no webhook, no polling).
- HTTP client: explicit connect and read timeouts; long read/write timeouts for uploads (50 MB multipart); connection pooling; no automatic retry of POSTs.
- Per-bot and per-channel throttling in front of the client, driven by `retry_after`. Serialise sends per channel so ordering is preserved and 429s are rare.
- Persist an attempt record **before** calling a send method and the resulting `message_id`(s) immediately after, so ambiguous outcomes are visible to operators.
- Media strategy: prefer multipart upload from bytes the service holds (deterministic errors, 10/50 MB) over URL sending (5/20 MB, depends on Telegram reaching the URL, PDF/ZIP-only for documents). Cache returned `file_id`s per bot to make re-sends free and size-unlimited.
- Handle `message_id == 0`: docs state that "in specific instances (e.g., a message containing a video sent to a big chat), the server might automatically schedule a message instead of sending it immediately. In such cases, this field will be 0 and the relevant message will be unusable until it is actually sent." The service then has no handle for edit/delete; record the post as "accepted, id pending/unknown".
- Self-hosted Bot API server (only if files larger than 50 MB are required):
  - Source: github.com/tdlib/telegram-bot-api, Boost Software License 1.0. No official Docker image was found in the README — **UNVERIFIED** whether one exists; expect to build from source or vet a third-party image.
  - Needs `--api-id` / `--api-hash` (or `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`), obtained per core.telegram.org/api/obtaining_api_id.
  - `--local` enables: uploads up to 2000 MB, unlimited downloads, upload by local path / `file://` URI, HTTP webhooks on any port/IP, `max_webhook_connections` up to 100000, absolute `file_path` in `getFile`.
  - Listens on plain HTTP, default port 8081 (`--http-port`); keep it on the internal Docker network or put a TLS proxy in front.
  - Migration: call `logOut` on the cloud API first; after that the bot "will not be able to log in back to the cloud Bot API server for 10 minutes". Moving between local servers uses `logOut` (or `close`, which returns 429 in the first 10 minutes after launch).
  - It is a stateful process (TDLib session data on disk) — needs a persistent volume, and one bot should be logged in to exactly one server.
  - Recommendation: start on the cloud API; make the base URL configurable so a local server can be introduced later without code changes.
- Monitoring: count of 429s and observed `retry_after`, 401/403 per destination (credential or membership lost), ambiguous-outcome sends, latency of upload calls.

## Known restrictions

Idempotency and duplicate risk:

- **There is no idempotency key, client-supplied message id, or dedup parameter on any Bot API send method.** The 10.3 reference contains no such parameter (searched for idempotency / random_id / dedup: zero matches). Each accepted `send*` call creates a new message.
- **Exactly-once publishing cannot be guaranteed.** If Telegram accepts the message and the HTTP response is lost (timeout, connection reset, proxy `5xx`, process crash between response and DB commit), the service cannot distinguish "posted" from "not posted":
  - The Bot API has no method to read channel history or list recent messages (the only `get*Messages` method is `getUserPersonalChatMessages`, unrelated).
  - A known `message_id` could be probed (e.g. `forwardMessage`/`copyMessage` to a scratch chat, or a no-op edit), but in the lost-response case the id is exactly what is missing.
  - `getUpdates`/webhook deliver `channel_post` updates for channels the bot is a member of, but whether a bot receives a `channel_post` for a message **it sent itself** is **UNVERIFIED** (believed not). Even if it did, updates are retained at most 24 hours, are consumed by a single poller/webhook, and are mutually exclusive between polling and webhook — a fragile reconciliation channel.
- Consequence for the service contract: the service can offer idempotency **only at its own boundary** (caller-supplied idempotency key → stored attempt → stored result). For the Telegram leg, the choice is between at-most-once (never retry an ambiguous send; surface state `unknown` for a human or caller to resolve) and at-least-once (retry ambiguous sends; accept occasional duplicate posts). Recommended default: at-most-once for ambiguous outcomes, automatic retry only for `429` and pre-send connection failures.
- Albums are not atomic from the caller's perspective under failure: the result is an array, and the same lost-response ambiguity applies to the whole group.
- A text+media post that exceeds 1024 caption characters requires two messages; a failure between them leaves a partial post.

Other restrictions:

- Posts by a bot in a channel appear as the channel; `from` "may be empty for messages sent to channels".
- Delete only within 48 hours of sending (documented). No documented way to delete older posts via the bot.
- Paid posts cannot be edited and must stay up 24 hours to earn.
- No scheduling parameter exists on send methods; scheduling must be implemented by the service. (Telegram may itself auto-schedule a large video in a big chat, returning `message_id` 0.)
- `file_id` values are bot-specific; they cannot be shared across bots/tokens.
- Cannot change media type when resending by `file_id`; thumbnails cannot be reused.
- URL-sourced documents: PDF and ZIP only.
- Animations cannot be part of albums; documents/audio cannot be mixed with photos/videos in an album.
- `error_code` and `description` values are explicitly not a stable contract.
- Bots cannot see messages from other bots; irrelevant for publish-only use.

SDK versus direct HTTP (state on 2026-10-09):

| Library | Language | Latest | Released | License | Bot API |
|---|---|---|---|---|---|
| python-telegram-bot | Python (>=3.10) | 22.8 | 2026-06-12 | LGPL-3.0-only | 10.0 natively (per README) |
| aiogram | Python (>=3.10,<3.15) | 3.31.0 | 2026-08-25 | MIT | 10.3 |
| grammY | Node/Deno | 1.46.0 | 2026-08-26 | MIT | 10.3 |
| telegraf | Node | 4.16.3 | 2024-02-29 | MIT | 7.1 — no release in over two years; avoid |
| go-telegram/bot | Go | 1.27.0 | 2026-09-11 | MIT | 10.3, zero dependencies |
| mymmrac/telego | Go | 1.12.1 | 2026-08-29 | MIT | 10.3 |
| go-telegram-bot-api/telegram-bot-api | Go | 5.5.1 | 2021-12-13 | MIT | 5.5 — unmaintained; avoid |

Opinion: **integrate directly over HTTP.** The service needs about eight methods (`getMe`, `getChat`/`getChatMember`, `sendMessage`, `sendPhoto`, `sendVideo`, `sendDocument`, `sendMediaGroup`, `editMessage*`, `deleteMessage(s)`), all plain JSON or multipart POSTs with one response envelope. The SDKs are bot *frameworks* (update dispatching, handlers, FSM, middleware) whose value is on the receiving side, which this service does not use. Direct integration gives full control over the things that matter here — retry classification, the no-auto-retry rule for sends, token redaction in logs, timeouts, rate limiting — and removes the need to track a new Bot API release roughly every month (9.5 → 10.3 between March and August 2026). If a typed client is still preferred, the actively maintained thin options are aiogram (Python), grammY (Node) and go-telegram/bot (Go); note python-telegram-bot's LGPL licence and avoid telegraf and go-telegram-bot-api.

## Official documentation references

URLs opened on 2026-10-09:

- https://core.telegram.org/bots/api — Bot API reference (10.3): Authorizing your bot, Making requests, Using a Local Bot API Server, Update/getUpdates, Message, LinkPreviewOptions, ChatAdministratorRights, ChatMemberAdministrator, ResponseParameters, InputMedia*, Sending files, getMe, logOut, close, sendMessage, Formatting options, Paid Broadcasts, copyMessage, sendPhoto, sendDocument, sendVideo, sendAnimation, sendPaidMedia, sendMediaGroup, getChat, getChatMember, editMessageText, editMessageCaption, editMessageMedia, deleteMessage, deleteMessages
- https://core.telegram.org/bots/api-changelog — version history (10.3 on 2026-08-24)
- https://core.telegram.org/bots/faq — broadcasting limits, 429, paid broadcasts, file size limits, update handling
- https://core.telegram.org/bots/features — BotFather, token generation, Local Bot API comparison table, test environment, paid media
- https://github.com/tdlib/telegram-bot-api — local Bot API server README
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/README.md
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/CMakeLists.txt — server version 10.3
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/telegram-bot-api/Client.cpp — error mapping, 429/409 handling, channel id formula
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/telegram-bot-api/ClientManager.cpp — 401 handling, server-side flood control
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/telegram-bot-api/Query.cpp — 429 body with `retry_after`
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/telegram-bot-api/HttpConnection.cpp — HTTP status and `Retry-After` header
- https://raw.githubusercontent.com/tdlib/telegram-bot-api/master/telegram-bot-api/telegram-bot-api.cpp — server options

Library state (not Telegram documentation):

- https://github.com/python-telegram-bot/python-telegram-bot/releases , https://raw.githubusercontent.com/python-telegram-bot/python-telegram-bot/master/README.rst , https://pypi.org/pypi/python-telegram-bot/json
- https://github.com/aiogram/aiogram/releases , https://pypi.org/pypi/aiogram/json
- https://github.com/grammyjs/grammY/releases , https://registry.npmjs.org/grammy
- https://github.com/telegraf/telegraf/releases , https://registry.npmjs.org/telegraf
- https://github.com/go-telegram/bot/releases , https://raw.githubusercontent.com/go-telegram/bot/main/README.md
- https://github.com/go-telegram-bot-api/telegram-bot-api/releases
- GitHub REST API (`api.github.com/repos/<owner>/<repo>` and `/releases`) for the repositories above plus mymmrac/telego and tdlib/telegram-bot-api

## Implementation in this service

Added 2026-10-09, after the research above. Code: `src/content_fleet/providers/telegram/bot_api.py`.

| Topic | What the implementation does |
| --- | --- |
| Methods used | `sendMessage`, `sendPhoto`, `sendVideo`, `sendDocument`, `sendAnimation`, `sendMediaGroup`, `editMessageText`, `editMessageCaption`, `deleteMessages` |
| Account settings | `chat_id` (required), `test_environment` (Telegram's test servers), `api_base` (a self-hosted Bot API server) |
| Uploaded media | Sent as multipart from the service's media store. Media given by URL is passed to Telegram to fetch, except documents |
| Albums | 2 to 10 items; the caption goes on the first item; one post id per message |
| Ambiguous send (timeout, connection reset, 5xx, unreadable reply) | Never retried. The publication becomes `outcome_unknown` |
| Safe to retry | 429 (waits `retry_after`), failure to connect, Telegram failing to fetch a media URL |
| Edit, delete | Run inside the consumer's request; "not modified" and "message to delete not found" count as success |
| Not implemented | Paid media, rich messages, explicit `entities`, reply markup, thumbnails, replacing media by edit, splitting text over 4096 characters |
| Checked how | A stateful fake that validates every request against the Bot API 10.3 field list. Run live on 2026-10-10 against a public test channel, through the Docker stack: `sendMessage` (HTML), `sendPhoto` and `sendDocument` (multipart upload), `sendMediaGroup`, `editMessageText`, `deleteMessages` all returned 200, post links were correct, and neither the bot token nor the consumer key appeared in the container logs. Not exercised live: video, animation, media by URL, rate limiting, a private channel |
