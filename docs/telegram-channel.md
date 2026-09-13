# Telegram channel — what we post and how

This document is the playbook for the public Telegram room that mirrors what
shows up on the runners feed. The code lives in `src/runner_web/telegram.py`
(formatters and `send_post`) and `src/runner_web/main.py` (dispatchers).
The strategy here is the single source of truth; if the code drifts, this
document is the thing to update first.

## Goals

- **Tight, on-brand.** Every post is Markdown V2, with bold tickers, emoji-coded
  state matched to the new list tags, and a single URL ahead of the body so
  Telegram renders a clean link preview on the entity page.
- **Per-event dedupe.** Each event is delivered once. Runners use
  `telegram_alert_deliveries(ticker, entered_at)`; reports and the build use
  `telegram_channel_posts(kind, subject)`. A retry only happens if the prior
  delivery row is `failed` and under `attempts`.
- **No floods.** A single dispatch batches everything that's pending and uses
  `announcement_batch_ready` to wait for `TELEGRAM_ANNOUNCE_BATCH_MIN` items
  or the `TELEGRAM_ANNOUNCE_DEBOUNCE_MINUTES` window before posting.
- **Single render pass.** Every kind of message is built by one function in
  `telegram.py`; dispatchers reuse those builders and the `send_post` helper.

## Kinds and triggers

| Post | Trigger | Dedupe key | Format helper |
|---|---|---|---|
| New runner alert | pulse entry without a delivery row, score ≥ `TELEGRAM_MIN_SCORE` | `telegram_alert_deliveries(ticker, entered_at)` | `format_runner_digest_md` |
| 4:20 Blaze Report (pre- or post-market) | frozen session report without a channel post for its id | `telegram_channel_posts(kind="market_report", subject=id)` | `format_market_report_post_md` |
| Event on tracked ticker | new SEC/NDX/news on a ticker tracked by the pulse | per-event delivery row keyed by `(kind, subject_key)` | `format_event_post_md` |
| Public Flash report | research commission in `visible=public` without a channel post | `telegram_channel_posts(kind="research_report", subject=public_id)` | `format_public_report_post_md` |
| New build | `APP_BUILD_SHA` changed and `TELEGRAM_RELEASE_ANNOUNCEMENTS=1` | `telegram_channel_posts(kind="release", subject=sha)` | `format_release_announcement_md` |

The daily report cap (Flash runs per runner) lives in
`TELEGRAM_RUNNER_REPORTS_PER_DAY` and is enforced before the queue.

## Render rules

- Messages are sent with `parse_mode=MarkdownV2`. Every user-controlled string is
  escaped with `escape_markdown_v2`. Numbers, emoji and ordinary punctuation
  are passed through.
- The URL that Telegram unfurls into a preview is **the first URL in the
  body**. We hand-place it: a ticker card puts `/t/{ticker}` first, a public
  report puts `/t/{symbol}` followed by `/research/{public_id}`, the pre/post
  briefing puts `/reports/{day}/{pre|post}` on the last line.
- `link_preview_options={"is_disabled": false}` is sent on every message.
  When Telegram returns a parse error on Markdown V2, `send_post` retries once
  without `parse_mode` so the notification never gets dropped.

## Why we dropped the Dash model narration

The previous version asked the chat agent to narrate the batched announcement.
That gave a varied voice but suffered two problems: a single source of truth
disappeared, and Telegram used whatever URL the model happened to cite for the
preview. The channel posts are now deterministic — the formatter has the
entity page URL ahead of the body — and the chat agent is reserved for chat.

## Tones, emojis, and the new app vocabulary

Action tags in the list and the message headers match each other. The
formatter states use the same color anchors the list does (RUNNING green,
SETUP blue, EXTENDED orange, AVOID red, WATCH neutral, PAUSED muted).

| State | Header emoji |
|---|---|
| RUNNING | ⚡ |
| SETUP | 🔵 |
| EXTENDED | 🟠 |
| AVOID | 🔴 |
| WATCH | ⚪ |
| PAUSED | ⏸ |

The batched update header is 🦁 (the channel mascot emoji). Runner digests are
🟢 (a runner). Briefing posts are 🧭. Public research is 📄. Events are
📰.

## How to turn a kind on or off

`TELEGRAM_RUNNER_ALERTS` toggles runners + batched dispatch.
`TELEGRAM_RELEASE_ANNOUNCEMENTS` toggles the build announcement.
`TELEGRAM_MEMECOIN_ALERTS` toggles the GIF/photo deliveries (separate helper,
unchanged in this iteration).

## Anti-flood watch

The batched dispatcher is the only channel. It posts when the batch rule allows
it and posts the entire batch as a single message. Per-runner reports are
queued by `_queue_telegram_runner_reports` and capped at
`TELEGRAM_RUNNER_REPORTS_PER_DAY`. If a hot day saturates that cap, the rest
wait until tomorrow.
