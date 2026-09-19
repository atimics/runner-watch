# Telegram channel — the rundown

This document is the playbook for the public Telegram room that mirrors what
shows up on the runners feed. The code lives in `src/runner_web/telegram.py`
(formatters and `send_post`) and `src/runner_web/main.py` (dispatchers).
The strategy here is the single source of truth; if the code drifts, this
document is the thing to update first.

## The shape we are going for

The channel is a **financial radio program**, not a feed. A feed dumps
everything that happened; a program has a rundown — a handful of recognisable
segments, each with its own voice and its own slot, so a reader who tunes in at
any hour gets something that feels produced rather than emitted.

Two rules carry most of the weight.

### One message, one card

Telegram renders **exactly one link preview per message** — the first URL in the
body, or whatever `link_preview_options.url` pins. Everything after that is
plain blue text nobody taps.

So: **every message carries exactly one URL, and that URL resolves to a page
with an `og:image` card.** Everything else in the message is text.

This is the rule the old batched announcement broke. A ten-runner batch emitted
ten `[$TICKER](…)` links plus a link per report — nine of them dead weight, and
the preview was whichever URL happened to sort first. The room got a wall of
markup with one arbitrary thumbnail.

Pages that already carry a card:

| Page | Card route |
|---|---|
| `/reports/{day}/{pre\|post}` | `/reports/{day}/{slug}/card.png` |
| `/t/{ticker}` | `/t/{ticker}/card.png` |
| `/research/{public_id}` | `/research/{public_id}/card.png` |

A memecoin replay is better than a card: `sendAnimation` puts the GIF itself in
the room, with the caption carrying the single link.

### One story per message, never a list

A list of tickers reads like a table, and a table is not a story. When several
things land at once the answer is never "put them all in one message": each one
takes its own turn, with its own card, spaced apart by the rundown clock.

So a busy morning reads:

> ⚡ **$SOUN is running** — gap up on 4× average volume → its own card
> …twelve minutes later…
> 📄 **New public report · $CAST** → its own card
> …twelve minutes later…
> 🔵 **$MSGM is setting up** → its own card

instead of one message with fourteen links in it. A session briefing follows
the same rule: it names who is out front — *$SOUN leads the pack* — and sends
the reader to the report for the rest, rather than printing a roster none of
which can be previewed.

The pacing is what makes this work rather than flood. `dispatch_telegram_posts`
plays **at most one segment per call** and refuses to play at all within
`TELEGRAM_SEGMENT_GAP_MINUTES` of the last message. The sweep worker calls it on
a timer, so a queue of five runners drains as five stories over an hour. A
runner that waited longer than `TELEGRAM_RUNNER_STORY_MAX_AGE_MINUTES` is
retired unheard: by then the move it describes is over, and stale news is worse
than none.

## The segments

Each segment has a slot, a single card, and a reason to exist. "Inventory"
means the thing that has to be pending for the segment to have something to
play.

| Segment | Slot | Inventory | The one card |
|---|---|---|---|
| **The Opening Brief** | 4:20 a.m. ET, appointment | frozen pre-market report | `/reports/{day}/pre` |
| **Runner story** | rotating, one at a time | runners without a delivery row | `/t/{ticker}` |
| **Flash Report** | rotating, staggered | research commission gone public | `/research/{public_id}` |
| **The Closing Bell** | 4:20 p.m. ET, appointment | frozen post-market report | `/reports/{day}/post` |
| **The Scoreboard** | after the close, daily | Flash's record, community calls | `/flash/record` *(needs a card)* |
| **Halt Desk** | interrupt | `market_events` halts | `/t/{ticker}` |
| **Filing Desk** | interrupt, capped | `market_events` EDGAR / disclosures | `/t/{ticker}` |
| **Memecoin Replay** | rotating | rendered replay awaiting delivery | the GIF itself |
| **Sports Desk** | rotating | game decisions, alpha, receipts | `/sports/game/{id}` *(needs a card)* |

### Why these, in this order

**The two 4:20 briefings are the anchors.** They are appointment listening:
same time every weekday, 20 minutes into pre-market and 20 minutes after the
close. Everything else rotates around them.

**Halts are the most radio-worthy thing that happens all day** and we currently
post none of them. `format_event_post_md` is already written and wired into the
batch builder; nothing dispatches it because `_activity_payload` never emits
events. The `market_events` table (`source`, `feed`, `event_type`, `ticker`,
`event_at`, `source_url`) is already populated by the `trading-halts`, `edgar`
and `house-disclosures` workers. This is the largest piece of unused inventory
in the system.

**The Scoreboard is what makes the rest credible.** A channel that only posts
entries is a hype feed. A channel that posts its own win–loss after the close is
a program. The data exists (`/flash/record`, `/calls`, `/receipts`); the pages
need `og:image` cards.

**Sports and memecoins are the variety.** They break up an all-equities
rundown, and they are already separate products with their own boards. Sports
lives on its own origin (`sports.rati.chat`) with `/sports`, `/sports/radar`,
`/sports/alpha`, `/sports/receipts` and `/sports/game/{id}` — none of which
have cards or any Telegram dispatch today. Memecoin replays are already the
best-produced segment we have.

## The rundown clock

Radio works because the hour has a shape. The dispatcher's job is not "send
what is pending" but "pick the next segment".

`next_segment` picks what plays next, and it is the whole of the variety rule:

- **Priority.** `SEGMENT_ORDER` puts session briefings first (appointment
  listening), then a published Flash report, a structured SEC filing, and the everyday runner
  inventory that fills the gaps between them.
- **Rotation.** While something else is waiting, the room never hears the same
  kind twice running. A runner follows the briefing; a briefing does not follow
  a briefing. No curation and no randomness — just "skip the kind you just
  played if you can".
- **The gap.** At most one message per `TELEGRAM_SEGMENT_GAP_MINUTES`, measured
  from the last successful text story. The shared channel schedule also paces
  attempts across text, build notes, and coin GIFs.

Still to add: **interrupts** that jump the queue for a halt or a material
filing, hard-capped per hour so a filing storm cannot become the whole program.

This replaced `announcement_batch_ready`, which optimised for the opposite
thing: it waited for items to pile up and then fused them into one message.
Batching was the right answer to flooding when every item was its own ping; a
paced rundown is the better answer, because it keeps every item's card intact.

## Dedupe and delivery

Every text story has a unique `(kind, subject)` in `telegram_outbox_items`.
Runners use ticker plus entry time; reports use their saved report ID; filings
use their transaction or stake ID; builds use their commit ID. Coin GIFs retain
their unique coin ID and saved replay. Each message keeps its original destination.

A database claim reserves the attempt before the network call. Confirmed sends
record the positive Telegram message ID and update the existing delivery tables.
Explicit rate limits schedule up to three attempts. Interrupted sends and lost
replies become `uncertain` for inspection. Queued runners past their news window
become `stale`. Each story keeps its exact saved Markdown body through retries.

## Render rules

- Messages are sent with `parse_mode=MarkdownV2`. **Everything that is not a
  deliberate markup character is escaped with `escape_markdown_v2` — numbers and
  ordinary punctuation included.** Telegram reserves ``_*[]()~`>#+-=|{}.!``
  everywhere in body text, not only at the start of a line: the period in
  `12.5%`, the sign in `+18.3%` and the hyphen in `8-K` each fail the parse on
  their own. Emoji pass through. A URL reaches the body only as an inline link
  (`markdown_link`); a bare URL is a run of reserved characters.
- **One URL per message.** If a message wants to point at more than one thing,
  it wants to be more than one message, or it wants a page that collects them.
- Blocks are assembled by `_join_blocks`, which drops a card whole rather than
  slicing the joined message at 4096 and stranding an open `*` or a trailing
  backslash. Anything trimmed to a length — release notes, the replay caption —
  is trimmed *before* escaping, for the same reason.
- Chat replies (`send_reply`) are a separate path: plain text, no `parse_mode`,
  and the persona prompt asks for prose without markdown so nothing lands as
  stray asterisks.
- Posts with a URL enable its link preview. Saved channel posts use strict
  Markdown delivery. A parse error is recorded for review. Direct callers may
  explicitly select the plain-text fallback with `allow_fallback=True`.
- `tests/test_telegram_format.py` checks escaped reserved characters, complete
  links, confirmed receipts, and the optional plain-text fallback.

## Build order

1. ~~**One-card rule.**~~ Done: every message carries one URL, and the roster
   became one story per runner.
2. ~~**The rundown scheduler.**~~ Done: `next_segment` plus the pacing gap
   replaced the batched merge.
3. **Turn on the desks.** Emit `market_events` into the dispatcher so the halt
   and filing segments have inventory; `format_event_post_md` and the
   `"event"` branch of `_render_segment` already exist, and nothing fills
   `activity["events"]`.
4. **Cards for the scoreboard and sports.** `og:image` plus a `card.png` route
   for `/flash/record`, `/calls` and `/sports/game/{id}`, mirroring
   `_ticker_card_png`, then their own entries in `SEGMENT_ORDER`.
5. **Per-segment daily caps**, so a hot day of runners cannot crowd out the
   quieter verticals once sports and the scoreboard are in the rotation.

## Why we dropped the Dash model narration

The previous version asked the chat agent to narrate the batched announcement.
That gave a varied voice but suffered two problems: a single source of truth
disappeared, and Telegram used whatever URL the model happened to cite for the
preview. The channel posts are deterministic — the formatter owns the
destination — and the chat agent is reserved for chat.

## Tones, emojis, and the app vocabulary

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

Segment headers: 🐆 the board and the build (the channel mascot, matching the
reactions and the chat persona), 🧭 the 4:20 briefings, 📄 public research,
📰 the filing desk, 🛑 the halt desk, 🪙 memecoin replays, 🏟 the sports desk,
🏆 the scoreboard.

## How to turn a kind on or off

`TELEGRAM_RUNNER_ALERTS` toggles runners + batched dispatch.
`TELEGRAM_RELEASE_ANNOUNCEMENTS` toggles the build announcement.
`TELEGRAM_MEMECOIN_ALERTS` toggles the GIF/photo deliveries.

The daily free-report cap lives in `TELEGRAM_RUNNER_REPORTS_PER_DAY` (20) and
the per-batch pick in `TELEGRAM_RUNNER_REPORTS_PER_RUN` (3), staggered by
`TELEGRAM_RUNNER_REPORT_STAGGER_MINUTES`.

## Anti-flood watch

`TELEGRAM_SEGMENT_GAP_MINUTES` (12) is the primary flood control: one message
per dispatch, never closer together than the gap, however much is pending.
`TELEGRAM_RUNNER_STORY_MAX_AGE_MINUTES` (180) drains the other end, retiring
stories the room would no longer care about instead of letting a backlog play
out hours late.

Per-runner Flash reports are queued by `_queue_telegram_runner_reports` and
capped at `TELEGRAM_RUNNER_REPORTS_PER_DAY`. If a hot day saturates that cap,
the rest wait until tomorrow.


### Confirmed delivery and evidence cards

Every story now passes through the saved outbox described in
[Telegram announcements](telegram-announcements.md). The outbox stores the exact
Markdown body and destination before an attempt. A positive Telegram message ID
confirms delivery. Rate limits schedule a retry; lost acknowledgements hold the
post for review. The shared channel schedule also covers GIFs and build notes.

New structured SEC filings enter the rundown as one story per transaction or
reported stake. They name the filers and their roles, label the reported action,
and keep dates, share classes, joint ownership, and amendments visible. The one
link opens the ticker map with its SEC evidence. The existing filing archive is
the starting baseline. The channel history is available at
`/telegram/announcements` with the operations access key.

Saved channel posts use strict Markdown delivery so their saved body matches
what was sent. The optional plain-text fallback remains available to direct
callers. Formatters bound source fields before escaping and retain complete links.
