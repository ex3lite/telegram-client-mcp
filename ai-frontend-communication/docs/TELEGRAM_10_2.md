# Telegram Bot API 10.2 integration

The integration targets aiogram 3.30 and the official Telegram Bot API available on July 14, 2026.
The authoritative references are the [Bot API changelog](https://core.telegram.org/bots/api-changelog),
[Bot API reference](https://core.telegram.org/bots/api/), and
[Guest Mode guide](https://core.telegram.org/api/bots/guest-mode).

## Implemented delivery paths

| User path | Initial response | Completion | Visibility |
| --- | --- | --- | --- |
| Private `/ask` | Native `sendRichMessageDraft` with a stable draft ID and Thinking block | On the first answer delta the stream is promoted to a permanent `sendRichMessage`, then edited in place; long ordinary answers continue in lossless Rich Message chunks | User and bot |
| Group `/ask` | Ordinary placeholder plus `sendChatAction(typing)` | `editMessageText` with `rich_message`; additional lossless Rich Message chunks only when needed | Group |
| Group `/ask_private` | Ephemeral reply bound to the incoming `ephemeral_message_id` | `editEphemeralMessageText` with `receiver_user_id` | Invoking user and bot |
| Group `/request` | Ephemeral acknowledgement when Telegram supplies ephemeral context | The request is durable in PostgreSQL/admin | Invoking user and bot |
| Guest query | Immediate `answerGuestQuery` publishes a rich placeholder and returns an inline message ID | `editMessageText` updates that inline message | Invoking chat |

When `telegram_streaming_enabled` is on, private generation refreshes one native Rich Message draft
through `sendRichMessageDraft`. Its deterministic, non-zero signed 31-bit draft ID comes from the
interaction UUID, so Telegram animates one draft instead of creating message spam. Thinking is sent
as `InputRichBlockThinking`. The Bot API defines that draft as an ephemeral 30-second preview, so a
15-second heartbeat keeps the latest safe Thinking state alive while Claude reads the snapshot or
uses tools. Once structured answer text appears, the worker sends a permanent Rich Message, stores
its message ID in the interaction and edits that same message for all remaining deltas and final
publication. Raw Thinking is never mixed into the permanent answer. Every draft and live edit is
checked against the current member policy and privacy-redacted before delivery. A draft API failure
is logged but cannot block the permanent answer. Telegram drafts are private-chat only, so groups
receive the official typing action and one persistent rich answer. The application keeps the durable interaction/job before
background generation, except the Guest Mode placeholder, which must be answered immediately so
Telegram returns the inline message ID needed for later editing.

The permanent answer is not a replayed fake animation: it is published only after Claude's context
receipt, exact-snapshot citations, live authorization and final privacy checks pass. Ordinary long
answers are split on paragraphs/newlines without losing text. `.md` is generated and attached only
when the user explicitly asks for documentation, a specification, guide, runbook, report or file.
The server decides that intent before Claude starts and rejects both a promised-but-missing file and
an unsolicited artifact instead of silently dropping either one.

## Command and update transport setup

`dca-bootstrap telegram-setup` registers these scopes and configures `DCA_TELEGRAM_MODE`:

- private chats: `/ask`, `/request`, `/help`;
- group chats: `/ask`, ephemeral `/ask_private`, ephemeral `/request`.

Production uses `polling`: setup calls `deleteWebhook(drop_pending_updates=false)`, then the native
worker long-polls through `DCA_OUTBOUND_PROXY_URL` concurrently with its durable job loop. `webhook`
mode remains available; it requires `DCA_TELEGRAM_WEBHOOK_SECRET`, checks the secret header and body
limit, and receives updates at `/webhooks/telegram`. Both transports share the same PostgreSQL
`update_id` reservation and durable job enqueue, including the immediate Guest Mode placeholder and
its `delivery_uncertain` handling. Guest updates are included in the dispatcher-derived
`allowed_updates` list. A PostgreSQL advisory lock permits only one polling consumer; conflicting
consumers and invalid Telegram credentials fail the worker instead of being retried forever.

## Capability checks and limitations

- Run `telegram-setup` and require the expected `bot=@username` output. `supports_guest_queries`
  must be true before advertising Guest Mode. `has_topics_enabled` reports private threaded mode;
  enable it in BotFather only if the product needs private topics.
- In production require `telegram_mode=polling` and `webhook=deleted`; retained pending updates are
  consumed by the worker after it starts.
- Guest Mode works when Telegram enables the bot profile capability. Telegram documents it for
  non-secret private chats, groups, and supergroups, excluding protected-content groups. The caller
  must already be a verified project member in this application.
- Ephemeral commands require Bot API/client support. The application only chooses the ephemeral
  path when Telegram supplies an `ephemeral_message_id`; ordinary `/ask` remains the public group
  path.
- Telegram delivery timeouts are not proof of failure. External sends can become
  `delivery_uncertain` and require human reconciliation instead of automatic replay.
- Rich content currently uses text/Markdown and a thinking block. Bot API 10.2 media-rich blocks,
  voice notes, tables, collages, and slideshows are not needed for this workflow.

## Explicitly deferred official features

- Communities and their chat-added/chat-removed events;
- managed/secretary bot creation and access management;
- bot-to-bot messaging;
- business-account automation, join-request queries, and Mini Apps.

Add one of these only after a concrete product flow, authorization model, update contract, audit
events, and live Telegram smoke test exist. They are not prerequisites for the four MVP paths.
