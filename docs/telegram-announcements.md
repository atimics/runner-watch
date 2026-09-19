# Telegram announcements

Messages use saved runner metrics, public research titles, game names, and SEC transaction lines. Each card keeps its links. Each story gets one message within Telegram's 4,096-unit text limit. Formatting uses UTF-16 units, including emoji. Each stock filing card carries its named people, reported action, units, share class, dates, and a link to its ticker map and SEC evidence. Joint transactions stay shared. Each reported stake keeps its own person and share class. Amendments are labelled.

Migration 74 creates the text outbox and the shared channel schedule. It also marks the existing SEC filing archive as the starting baseline. Newly collected structured filings become eligible for stock cards. Restoring structured data for a baseline filing retains that baseline.

The outbox saves the destination, exact text, and included item identities before delivery. Item identities are unique across destinations. A database claim gives one process each send attempt. Success requires a positive Telegram message ID. Rate-limit replies set the earliest retry time, with a maximum of three attempts. Lost replies and interrupted sends become `uncertain` for inspection. An uncertain post retains its exact text and attempt history.

Runner stories, SEC cards, build notices, and coin GIFs share a channel schedule:

| Setting | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_CHANNEL_INTERVAL_SECONDS` | 720 | Minimum gap between channel attempts |
| `TELEGRAM_TICKER_QUIET_SECONDS` | 1800 | Minimum gap for the same stock, game, or coin |
| `TELEGRAM_CHANNEL_DAILY_LIMIT` | 24 | Channel attempts per UTC day |

The daily budget counts attempts, including uncertain deliveries. Rate-limit responses can extend the channel's waiting period. A pending card keeps its place until its ticker and channel become eligible. The rundown keeps its story rotation and 12-minute gap. The channel interval defaults to the configured rundown gap. Queued runners expire after their news window. Coin delivery keeps its immutable replay, GIF checks, and captured destination. Build notices retain their own enable switch.

`/telegram/announcements` shows queued text and recent delivery history. Its form sends the operations access key in an Authorization header to `/api/telegram/announcements`. The history response uses `Cache-Control: no-store`. The page keeps the key in the current form only. Both text and coin records show their status and Telegram receipt when available. Earlier coin posts retain their saved replay and receipt; new attempts also save their exact caption.

Validation covers concurrent processes, restart recovery, lost replies, explicit rate-limit retries, destination changes, Unicode boundaries, partial queue delivery, the shared coin/text schedule, SEC baselines, joint filers, amendments, stakes, and access control. Phone and desktop browser tests check the history page and safe rendering of message text.
