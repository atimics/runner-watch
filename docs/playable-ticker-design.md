# A game inside three ticker screens

Design proposal, 12 September 2026. Based on PR #229 at `a5810f52322a1a36afa1523c8e75f31c7c4b6d9c`, compared with main at `0d3948889c3576e797b9b4111a4defeded4a7fb8`.

This is the first proposal. The [revised assessment](research/game-pattern-assessment.md) follows a source review across the games in `~/develop`. It replaces the Finish chapter step with a lasting result record, adds explicit story questions and follows, and places cosmetic progression behind a product study. The current delivery scope lives in [backlog #232](https://github.com/atimics/runner-watch/issues/232).

The core loop is **spot a story → read Flash → make a Call or React → return for the outcome → finish a chapter**. A ticker becomes a story with a beginning and an ending. Its price or score remains the main visual.

This proposal includes a clickable sample and a separate [adversarial review of the base](reviews/pr-229-base-design.md). It describes future work. The production app is unchanged by this document and preview.

## The product contract

There are three screen types: **Ticker List, Ticker Detail, Ticker Map**. Stocks, Memecoins and Sports use the same shell, row structure, action placement and state language. Market data supplies the values and available choices. A sports score and a price chart occupy the same visual slot.

Reports, comments, My Calls and completed chapters live within these screens. A report replaces the body of Detail. My Calls changes the scope of List. The account control opens a small sheet. Every deep link resolves to this shared shell.

Each screen has one main visual and one main action. Supporting content earns its space by helping someone choose or understand an outcome. Provider names, receipt indexes, wallet dumps, generation logs, model settings and internal scores stay in the service layer. A current price, a pending score, a report's publication time and a Call's settlement rule are useful product information.

RATi supplies the words. Players choose actions. A generated voice is identified as AI; its provider stays internal. The existing [media game intent](media-game.md) supplies the starting point. The current user direction supersedes that document's suggestion to expose sources.

## What the app already contains

These are code findings at the reviewed revision, rather than claims that every feature is active in production. A read-only check of the live `/reports` page on 12 September showed scheduled briefings, avatar comments, report history, an unavailable review and the older navigation. The live product still differed from PR #229.

| Capability | Existing behavior | Design implication |
| --- | --- | --- |
| Calls | All three markets have paper Calls and settlement. Stocks and coins support rising-price Calls. Sports supports a team choice with frozen odds. | Use one Call component whose choice labels, entry and deadline come from the subject. |
| Flash | Daily claim: 100. Report: 100. Reaction: 10. Early report publication: +50. Winning Call reward: up to 50. The ledger deduplicates rewards and charges. | Show balance in the account control and exact cost beside each paid action. |
| Reports | Scheduled stock briefings and recaps; commissioned stock and sports reports. Managed reports have a one-hour private window, then become public. | Free public reading is the usual entry. Paid creation appears when a fresh report is needed. |
| Reactions | Stock and sports comments are generated from an empty action request. Pending requests survive retries and reloads. | Restore one `React · 10 Flash` action and a compact result. |
| Characters | Stable player pseudonym, portrait seed and one of six analysis abilities. A saved level defaults to 1. | Use a familiar face and voice. A working progression engine is new work. |
| Record | The public caller page combines Calls from all markets. Its daily machine comparison and streak are calculated from stocks. | Reuse the unified record. Matched competition needs a new outcome contract. |
| Map | Saved stock and coin graphs retain actor identities, subject ties, roles and times. Sports has team/event relationships. | Render actual connections with one simple explanation for the selected link. |

Important gaps: coin reports and player reactions need subject support; sports reactions need the same public-report context as stock reactions. The current report and comment views expose internal fields. Reusing those views would restore the clutter. Their services can support a new public presentation.

Evidence: [Flash rules](../src/runner_web/flash_wallet.py#L10), [commission creation](../src/runner_web/main.py#L6054), [reaction creation](../src/runner_web/main.py#L9592), [stock reaction context](../src/runner_web/main.py#L9336), [sports reaction context](../src/runner_web/main.py#L9409), [unified caller data](../src/runner_web/main.py#L3410), [avatar persistence](../src/runner_web/pseudonyms.py#L187), [saved graph](../src/runner_web/market_actors.py#L525).

## 1. Ticker List: a reason to open a story

Keep the base's market selector, List/Map switch, search and quiet rows. Each row contains an identity, price or score, its time context and **one story cue**. The cue replaces secondary filler text. Priority is: your result is ready; your Call is open; a public Flash report is ready; one useful market change.

Example: `LUMA · $4.20 · +5.0%` with `Your result is ready`. Another row says `Flash: volume rose as the move slowed`. The name remains available in the row's accessible label and Detail. A result cue is tied to a Call the player explicitly made. Public discovery ordering remains shared.

A small avatar and Flash balance occupy the existing account position. Its sheet contains the daily claim, My Calls and completed chapters. My Calls uses this same List with a clear scope title and a return to the market. A completed chapter opens the original ticker's result state.

Scheduled market briefings enter through one quiet `Today's Flash brief` action in List. It expands a short market summary in place, with subject links. Individual ticker Detail receives the relevant excerpt. A full archive is a date scope of the same List. This preserves the three screen types and keeps the market summary connected to tickers.

## 2. Ticker Detail: one story, one decision

The stable order is identity, main visual, Flash's short take, and the action area. A Call in progress or a result replaces the idle action area. The latest avatar reaction appears directly below Flash's take. Two visible reactions are the upper limit; the rest open as a focused Detail body.

The price chart includes its period and start/end prices. A sports event shows start time, then a confirmed score. `Price paused` and `Score pending` are explicit states. The visual, Call return and allowed actions update together.

**Read.** `Read report` replaces the chart-and-preview body with a calm reading view. Keep the ticker header and `Back to chart` or `Back to score`. The report has a headline, a short take, what changed, what would change the take, and when to revisit it. Existing useful long-form analysis remains available as readable paragraphs. Public corrections remain attached to the story. The footer contains one main Call action and a quiet React action.

**Commission.** If a report is needed, `Ask Flash · 100` opens a cost confirmation. After acceptance, show `Flash is writing` in the same story slot. The player can continue browsing. The ready state offers an explicit open action. A failed job restores the balance and offers a retry. An owner's managed private report shows `Yours until 2:30 pm` and an optional `Publish now · +50 Flash` confirmation. The customer-owned private report policy stays intact. Each visibility state comes from the report service.

**React.** `React · 10 Flash` shows that an AI avatar will post a public reply. One acceptance starts the existing durable request. A small pending bubble becomes one reply with the avatar's name and time. Reloading preserves the pending action. A failed request uses the existing refund and retry flow. The first version uses the existing single action. Extra commands such as `Check the risk` would require a new server contract and deserve their own design test.

**Call.** The confirmation shows the actual choice, entry, settlement rule and reward terms. Stock and coin choice text can be `Price rises`; sports supplies the two teams. The component stays the same. The service refreshes the preview when a quote or sports line changes before confirmation. A successful Call shows the recorded terms immediately. `Your Call: Owls to win` is recognizable on every return visit.

**Resolve.** Detail marks the entry and finish, shows the chosen side, result and earned Flash, then gives one short Flash debrief. `Finish chapter` records that the player reviewed the outcome. The monetary reward has already been credited once by settlement. Completing a losing chapter earns the same chapter progress as completing a winning one.

The actual settlement rules matter: stocks can close manually or settle at session close; coins can close manually or expire after seven days when a valid mark is available; sports settles at the final result. These appear in the same position with plain market-specific text. Higher/lower choices for prices and a shared timed duel are future changes to the Call rules.

Evidence: [stock Calls](../src/runner_web/calls.py#L66), [stock settlement](../src/runner_web/calls.py#L407), [coin Calls](../src/runner_web/memecoin_calls.py#L109), [coin expiry](../src/runner_web/memecoin_calls.py#L236), [report publication](../src/runner_web/main.py#L10047).

## 3. Ticker Map: follow a meaningful connection

The Map answers **“What else is connected to this ticker?”** A selected ticker connects to named public entities or qualified groups, then to related tickers. A shared entity is one shared node. Each selected connection gets one plain sentence and a time context. Tapping a related ticker opens its Detail.

The renderer, node roles and interaction stay the same in all markets. Stocks can connect through a disclosed holder; coins through a possible funding link; sports through a shared team. Those are different facts expressed through the same visual language. The Map only draws supported edges. A sparse graph receives a useful empty state.

Wallet clusters are uncertain observations. Wording such as `These wallets may share an initial funder` preserves that uncertainty. Strong insider or wash-trading claims require stronger evidence. A character portrait gives a recurring entity a face; it keeps its role as a fictional representation clear. Browsing uses cached portraits or a simple fallback. Background portrait work follows a separate bounded service budget.

A focused Map connection can later supply a chapter about related events. The first release makes the graph useful before adding collection rewards to it.

## Progress and the return visit

Keep two concepts distinct. **Flash buys media actions. Chapters record completed stories.** A player can understand both in one sentence. Existing winning-Call rewards continue under their current rules: positive stock return percentage ×10, coin return percentage ×1, and sports odds-based reward, each capped at 50 and rounded by the ledger.

First proposed milestone: five reviewed chapters unlock a small avatar frame. The preview demonstrates this choice. The exact milestone is a design setting for testing. Chapter count records participation; it says nothing about financial expertise. Earn one chapter per account and completed story episode, including losses. Use an explicit review action and a stable episode ID so replay and multiple Calls cannot farm the same chapter.

The first return cue is `Your result is ready` in List and My Calls. It opens the result directly. Optional notifications require the player's chosen delivery setting and link to that same state. Unresolved Calls retain their pending state until a valid outcome arrives.

`Beat Flash` belongs in a later release. Both sides need the same subject, entry window, outcome horizon and scoring rule, fixed before the event. Current aggregate stock comparison is insufficient for that duel. A dependable comparison can then support a short record alongside the Call; broad leaderboards can follow observed demand.

## Repair the base before expanding it

The independent review found six P2 issues. Its full evidence and reproductions are in the [review](reviews/pr-229-base-design.md).

1. Quote refresh can leave the Call return and button frozen.
2. A saved coin price can look current on List and Map.
3. A delayed sports feed can show a false 0–0 score.
4. Call selection, entry, settlement terms and earned reward disappear.
5. Map turns relationships into anonymous dots.
6. Chart shape omits the magnitude of the plotted move and a useful accessible summary.

The review found no demonstrated P0 or P1 failure. Passing tests cover the new layout and a failed Call action. Successful Call, settlement, recovery and real relationship journeys need focused coverage through the actual routes. Old sports-template tests can pass while the current route uses another template.

## Delivery order and checks

**A. Restore the decision loop.** Repair the six issues and keep the current quiet layout. Introduce a small public detail state so price/score, chart, Call and allowed actions refresh together. Acceptance: a chosen team remains visible; a quote change updates the displayed return; pending data stays honest; earned rewards survive reload; Map explains a real edge.

**B. Bring reports and reactions into all three markets.** Add coin subject support, sports report context and shared presenters for reports, reactions and Calls. Use a typed subject identity rather than sending coin mints through a stock ticker path. Keep the existing charge ledger, request keys, access checks and refund behavior. Old deep links resolve into the new Detail state. Acceptance: the same read, commission, react and Call journey works for each market.

**C. Ship the return journey.** Put My Calls into List, add the result body and an explicit chapter-completion record. Add a modest avatar milestone after that works. Acceptance: win, loss and delayed settlement each have a clear ending; a repeated review gives one completion; Flash reward and chapter progress remain independently correct.

**D. Test matched competition.** Add a fixed comparison contract for Flash and player Calls. Use the results of the first three steps to decide whether a duel improves return visits.

Each release should pass signed-in and signed-out journeys at phone and desktop widths; free, paid, locked, pending and failed report states; reaction retry after reload; stale-to-current recovery; upcoming, live and final sports events; successful Call creation and settlement; long names; empty charts; and keyboard operation. Assert internal-field exclusion in initial HTML, embedded state and refreshed JSON. A clean first render alone leaves gaps.

Observe explicit completed actions and aggregate service outcomes within the existing privacy policy. Use completed-Call reviews within 48 hours, first report-to-Call completion, and delayed-action recovery as early measures. Establish a baseline before choosing a retention target. A small usability check should establish whether players can identify the main action, their chosen side and the result unaided. Spending totals are business usage, while chapter progress measures completed stories. Preserve account deletion/export and the existing limits on passive activity tracking.

## Preview scope

The [interactive source](playable-ticker-preview.html) uses fictional tickers, prices, connections and stories. It demonstrates the shared List, Detail, Map, report reader, paid-action confirmation, Call result and chapter milestone. The first ticker in each market has a result ready; the other rows support starting a Call. Its local state simulates selected journeys; the services and missing market support described above remain implementation work. The optional preview control compares a story headline with a report label in List. It belongs to design review, outside the product UI.
