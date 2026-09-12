# PR 229 adversarial design review

Reviewed head `a5810f52322a1a36afa1523c8e75f31c7c4b6d9c` against base `0d3948889c3576e797b9b4111a4defeded4a7fb8` on September 12, 2026. Scope: changed routes, adapters, shared template and script, old user flows, browser tests, and committed desktop/phone previews. Screenshots contain sample data. Two live-update cases were reproduced in isolated Chromium with mocked responses; other cases were reproduced with the production adapters. This is a read-only review.

The shared shell is clear and easy to scan. It is a useful layout foundation. The current content supports browsing prices and scores; the decision and result loop needs repair before adding progression. The strongest issues are below. Each is P2: a concrete issue to address in the next patch. There is no demonstrated release-wide P0 or P1 failure.

## 1. Detail updates the price while the Call stays frozen

**Base defect.** Open a stock or coin detail with an active Call, then leave it open through a quote update. `refreshQuote()` updates price, change and time. `refreshSurface()` skips both markets. The Call return, action availability and chart keep their initial state.

Reproduction: entry $1.50, initial price $1.56 and Call return +4.0%. A fresh quote returns $3.00. After two one-minute refresh cycles the header says $3.00 and Your Call still says +4.0%. The actual return at that price is +100%. A second test starts a coin with `can_call=False`; a fresh quote arrives, but the Call button stays absent after two refresh cycles.

**Impact:** the user can close a Call based on an old displayed return, or wait indefinitely for a Call action that became available. Automatic stock settlement can also leave an obsolete Close Call button on screen.

**Remedy:** refresh one small public detail state containing the quote, chart range, current Call result and allowed actions. Update these together. Preserve the open confirmation and keyboard focus.

**Sources:** `web/static/market-screen.js:25-35,50`; `src/runner_web/market_screens.py:257-284`. The earlier `web/static/memecoins.js:169-192,244-245` refreshed availability, history and Call results together.

## 2. Saved coin prices look current on List and Map

**Base defect.** The new default coin board calls `memecoin_market(view="radar")`, which includes stale saved rows. The adapter drops `stale`, and both List and Map omit the saved quote time. An old price and its old 24-hour change keep the same bright presentation as a current one.

Reproduction: render the same coin once with a current timestamp and once with a January timestamp plus `stale=True`. The List HTML is identical. This is an intentional change in the input set: the previous default Pulse filtered stale coins.

**Impact:** a user scanning the discovery list cannot tell whether the displayed move belongs to the current market. A successful page refresh can keep serving that old quote.

**Remedy:** preserve a simple business state such as `Price paused`, with a muted value, or keep stale rows out of the active list. Quote age belongs to the decision. Provider names and diagnostic text remain internal.

**Sources:** `src/runner_web/main.py:6781-6783`; `src/runner_web/memecoins.py:439-445`; `src/runner_web/market_screens.py:84-101`; `web/templates/market_screen.html:29-31`.

## 3. A game awaiting its score can show a fabricated 0–0 headline

**Base defect.** The shared sports row uses `started` to decide whether to display numeric scores. The existing state builder also sets `started=True` when the scheduled time passes, even while the saved status is still pregame. Saved pregame zeros then become a visible score despite `score_available=False`.

Reproduction: a pregame event after its scheduled start with both saved scores set to zero. The production state builder returns `score_available=False`. The new detail headline shows `0 – 0`; its two team rows correctly show `—`.

**Impact:** List, Map and the detail headline can claim a score the app has not received. Detail contradicts itself.

**Remedy:** use the same `score_available` rule for the headline and team visual. Show `Score pending` until a real score is available. Keep the scheduled start time in the shared secondary text so upcoming games are distinguishable.

**Sources:** `src/runner_web/market_screens.py:65-70,226-228`; `src/runner_web/sports.py:3375-3408`.

## 4. A Call loses the information needed to recognize it and understand its result

**Base defect with a direct game-loop consequence.** After a sports Call, the adapter keeps only `Your Call · Open/Win/Loss`. Selection, frozen line and reward are discarded. Opposite picks on the same matchup produce identical public screens. For stocks and coins, entry price/time disappear, and a successful close immediately reloads the page, discarding the response's reward and result.

The confirmation says only that a paper Call will be recorded. It uses the same text for opening and closing. It gives the user no settlement time or rule, and no frozen sports line or possible reward.

**Impact:** the user cannot verify the team they chose from the ticker detail, or understand the payoff. A successful Call has little feedback. The new My Calls link eventually exposes the older profile layout, which contains the record and reward but breaks the visual continuity.

**Remedy:** one compact shared Call state: chosen side or direction, entry, current result and next settlement. Confirm against a simple preview. After success, show the recorded result and earned Flash in place. Keep this inside Detail and use the same visual contract in all markets.

**Sources:** `src/runner_web/market_screens.py:235-244,260-284`; `web/templates/market_screen.html:44-46`; `web/static/market-screen.js:64-66`; existing value in `web/templates/ticker.html:182-195`, `web/templates/sports_game.html:72-106`, and `src/runner_web/main.py:9970-9975`.

## 5. Map uses graph-shaped decoration without showing the relationships

**Product defect; choose its meaning before building progression on it.** The adapter uses only the number of actor IDs attached to each ticker. It drops shared actor identity, link direction, role, weight and recency. Every ticker receives its own ring of anonymous dots. Selecting any part opens the same ticker detail. Sports always receives two team satellites.

Reproduction: the public map data for 12 and 30 actor IDs is identical. Production already caps the actor list at 14, and the new view further caps the dots at 12. More fundamentally, two tickers connected to the same actor look like two unrelated stars. There is no label, legend or available action that explains the dots.

**Impact:** Map provides less scanning capacity than List and gives the user no additional decision. Its geometry suggests a relationship view that it cannot explain. The screenshots confirm the dots dominate the screen while meaning remains absent.

**Remedy:** define a single question the map answers, such as "What else is connected to this ticker?" Use meaningful public relationship labels and a selectable related ticker. Reuse one node/link vocabulary across markets. Keep receipt IDs and raw wallet details inside the backend. If a relationship is uncertain, use plain qualified language rather than an insider allegation.

**Sources:** `src/runner_web/market_screens.py:123-157`; `web/templates/market_screen.html:29`; richer saved relation data in `src/runner_web/market_actors.py:525-550,563-597`. Visual evidence: `docs/market-screens/stocks-map.png` and `sports-map.png`.

## 6. The price chart shows shape without magnitude or an accessible summary

**Product and accessibility defect.** Every series expands its own minimum and maximum to the full plot height. A tiny gain and a doubling can produce the same line. The only labels are the start/end times; the large percentage above is the quote's daily change, which can cover a different period. The SVG's accessible text is only `Price history`.

**Impact:** the user can see direction but cannot judge the move in the plotted period. A screen-reader user receives no useful chart result. The phone screenshot makes the unlabeled line the largest object on the page.

**Remedy:** retain the simple line. Add compact start/end prices or a range gain beside the time range. Give the SVG the same brief text summary. This fits the existing space and makes the visual useful.

**Sources:** `web/static/market-screen.js:14-22`; `web/templates/market_screen.html:38-41`; `docs/market-screens/phone-detail.png`.

## What belongs to the next design

Reports, avatar reactions, public Calls, Flash rewards and the user's record are existing product value. Their removal from these three screens was part of the requested simplification. Restore them through a shared Detail contract and focused states, rather than treating each old panel as a required component.

A small complete loop would be: find an interesting ticker; read one clear report view; make a Call or summon a reaction; return for the outcome; spend earned Flash on the next useful action. List needs a compact reason to open a ticker and a personal outcome cue. Detail needs the story, the Call and the result. Map needs a meaningful connection. All three markets should use that grammar, while preserving honest differences in their settlement rules.

## Integration paths that would expose internal information again

- `_flash_comments.html:21` prints the exact generation model. `web/static/flash-comments.js:56-60` recreates that label after refresh. Removing it from only the initial HTML would leave the leak.
- `research_report.html:50-52` shows the report's model and model ladder position. Lines 147-153 add source indexes and receipt summaries; line 175 adds model names, source-family counts, freshness counts and linked-claim counters. A simple link to this old report page would leave the minimalist contract immediately.
- `mobile_base.html:35-48`, reached through My Calls and old report pages, restores the old profile sheet and an AI model setting. This remains outside the new shared shell.
- The current stock and sports detail routes still compute the old report and comment context. The new wrappers simply ignore it. Keep a public report/reaction adapter between those backend objects and every HTML/JSON response used by the new UI.

## What the green tests establish

The new browser tests establish shared navigation, row visibility, lack of horizontal overflow, clickable map centers, chart rendering and a failed Call confirmation path. They do not cover a successful create/close/settle loop, stale-to-current recovery, changing Call returns, or useful map relationships (`tests/test_browser_market_screens.py:38-98`). Several sports detail tests still render the retired `sports_game.html` directly (`tests/test_browser_sports.py:274-304`). They can pass while the route now renders `simple_sports_detail.html`.

Add focused journey tests through the actual routes for the six cases above. Keep internal-field sentinels in both initial pages and refreshed report/reaction responses. Review long names, empty history, stale quotes, upcoming/live/final games and signed-in outcomes with real saved data before describing the whole product as unified.

The review used isolated browser responses and the production adapters for its reproductions. Application files were left unchanged during the review.
