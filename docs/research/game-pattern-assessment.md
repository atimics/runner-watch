# Revised assessment: RATi as a story you take part in

12 September 2026. This assessment revises the [first design](../playable-ticker-design.md) after a source review across `~/develop`. The groomed work lives in [backlog #232](https://github.com/atimics/runner-watch/issues/232).

**The strongest direction is a short story with a question, a player choice, and a remembered outcome.** The existing three screens can carry that whole experience. The next design should make the result feel worth returning for. Character milestones follow a useful story record.

**Pseudonymity supplies the shared cast.** A familiar avatar can appear across markets while research learns more about the participant behind it. The [identity design](pseudonymous-identity-design.md) adds public wallet attribution, shared control, accepted aliases and later corrections. A reveal becomes a material story update. Stable faces and preserved history give that update meaning.

An accepted identity combination now creates a **new combined avatar**. CosyWorld's older breeding flow supplies the creative pattern: familiar traits, a fresh character and an introduction. RATi ties this moment to the accepted discovery and preserves the original faces as ancestry. The identity design specifies inheritance, repeated combinations, corrections and the shared reveal.

## What changed in the assessment

| First proposal | Revised decision | Reason |
| --- | --- | --- |
| Discover, read, Call, then press Finish chapter | Discover a question, Call or follow it, then return to its outcome | The games connect progress to a real action and consequence. The result itself supplies closure. |
| Five reviewed chapters unlock a frame | Keep an automatic record of resolved stories; test cosmetic milestones later | A count is a weak reward until the individual remembered moment matters. |
| Restore a generic generated reaction | Restore its reliable flow, then test a small choice of purpose | Player agency comes from a choice that changes the response. |
| Treat reports as readable content inside Detail | Give the report a question, a revisit trigger and a continuing story identity | A useful return visit requires a specific unresolved question. |
| My Calls is the personal List | Use a Your stories scope for explicit follows and Calls; retain a Call filter | Readers can return for an answer before choosing a price direction or team. |
| Add a compact avatar and its level | Give a small recurring cast distinct roles and continuity | Character value comes from recognizable responses and remembered positions. |
| Gamification after the base fixes | Define the story contract alongside the base fixes, then complete one shared journey | The story identity affects reports, reactions, return cues and the record. |

The first clickable sketch remains a record of the earlier proposal. Its Finish chapter control and five-chapter frame are experiments. This assessment and the current issue bodies govern the next implementation.

## Research scope and evidence

The core comparison covered five games: Ruby High, CosyWorld, Signal, Crownless and GLM Roguelike. I also read the Sector One companion, Swarm, Trebuchet, Grimoire and the shared UX guide for adjacent patterns.

The review used local committed source, tests and design records. Some checkouts are feature branches. Ruby High, GLM Roguelike and Grimoire also contain active local edits; implementation findings use their committed snapshots. The source revisions and file references are listed below and in [the source index](game-pattern-sources.json).

This establishes implemented mechanisms and recorded design choices. It provides no new player-retention measurements. I did not run these games or their test suites in this research pass. Tests cited here show what their authors check. The retention effects proposed for RATi remain hypotheses to test.

## Patterns that transfer

### 1. Give one meaningful result the spotlight

Ruby High's class design centers on an answer, a teacher response, classmates reacting and a changed school record. Its ritual budget gives one major moment the spotlight and keeps related rewards within that moment. The committed service tests verify that a class result contains the player's answer, teacher observation and consequence, and survives service restart. Its graduation code carries a remembered teacher response into the diploma. These are concrete links from action to result to memory. [Ruby design](https://github.com/cenetex/app-ruby-high/blob/7ca0c5e9cdefaa2b67a9633c1bc60b3e575ac942/docs/game-redesign-class-spine.md#L8), [result persistence test](https://github.com/cenetex/app-ruby-high/blob/7ca0c5e9cdefaa2b67a9633c1bc60b3e575ac942/src/__tests__/ruby-high-service.test.ts#L5326), [remembered response](https://github.com/cenetex/app-ruby-high/blob/7ca0c5e9cdefaa2b67a9633c1bc60b3e575ac942/src/services/ruby-high-service.ts#L8062).

**RATi application:** the completed Call is the main moment. Show the original choice, entry, final outcome, earned Flash and one specific response together. The record updates there. An avatar milestone can appear as a small part of that result when warranted. This keeps the emotional payoff in one place.

### 2. Let state changes create progress

Signal's worker story advances through hailing the right station, crossing a signal gap, placing and activating an outpost, completing a delivery and returning. The transition functions check the current step. Its tests explicitly reject claiming the return before the route is built. Progress follows work performed in the world. [Story transitions](https://github.com/cenetex/signal/blob/b8235a1236f7c5a391912b4e3225ef0eb659febd/shared/story_loop.c#L22), [ordering test](https://github.com/cenetex/signal/blob/b8235a1236f7c5a391912b4e3225ef0eb659febd/tests/c/test_story_loop.c#L58).

The smaller GLM Roguelike gives the same lesson with fewer systems: moving onto treasure changes the map, updates the count and produces the winning state at the goal. Using a shrine changes that tile into a used shrine. The action and its lasting effect are easy to connect. This is a useful small-game comparison; its local checkout has newer uncommitted work, so the source finding is pinned to `74a87487cf6b`, `roguelike.c:337–365`.

**RATi application:** a story reaches a result because a declared condition occurred. A recorded Call settles through its existing market rules. The lasting story record is created once from those events. Reading that result can mark it reviewed as part of the user's explicit open action. A separate collection click adds little value at this stage.

### 3. A small public view can sit over an exact engine

CosyWorld's current public-state test bounds the action hand to three entries and checks that rules context, internal memory, raw journal beats and other service fields stay outside the player state. Its current code publishes daily Journal pages from stored state and guards their publication identity. This is a stronger foundation for simplicity than hiding fields after they reach the client. [Public state contract](https://github.com/cenetex/cosyworld/blob/d76defb4e6644d5f6d0718906789da7ef211c839/v2/orchestrator-rust/src/tests/action_hand_tests.rs#L2197), [page publication and view](https://github.com/cenetex/cosyworld/blob/d76defb4e6644d5f6d0718906789da7ef211c839/v2/orchestrator-rust/src/daily_journal.rs#L252).

There is also useful evidence of design drift. CosyWorld's Journal document describes a page-image-only surface, while the current implementation also carries chat excerpts. Its Story Hand document labels the newer entity-pile layout as specified rather than implemented. I take the bounded public-state and durable-record patterns as evidence; the exact layouts need their own current verification.

**RATi application:** the service produces the next useful action, useful facts and result state for a typed subject. One shared presenter handles all three markets. Internal source evidence remains available to the research service and stays outside the customer payload. A plain `Price paused` state communicates the business fact directly.

### 4. Keep commitment and consequence together

Crownless's implemented trade flow compares the current quote with the one presented before it commits. Its receipt remains with the trade result. The Company Book returns to the context from which it opened and keeps completed work in its history. This closely matches the missing Call terms and disappearing result found in PR #229. [Implementation record](https://github.com/atimics/crownlesscarriage/blob/e5251dc6d582673e3481fce4f3ad35e52a077619/docs/design/interaction-ux-implementation.md#L19), [quote comparison](https://github.com/atimics/crownlesscarriage/blob/e5251dc6d582673e3481fce4f3ad35e52a077619/src/client/cc_adventure.inc#L580).

Crownless also has larger story expansions marked as future work. Its Last Fare plan starts with one complete journey before widening the content. That is a useful scope rule, rather than evidence that the expansion is playable today. [Expansion plan](https://github.com/atimics/crownlesscarriage/blob/e5251dc6d582673e3481fce4f3ad35e52a077619/docs/design/the-last-fare.md#L1).

**RATi application:** a changed quote or sports line refreshes the confirmation. The result preserves the choice and terms after reload. The first release proves one complete journey per market through the same screen contract.

### 5. Characters need a role and something to respond to

Ruby High's visual direction gives the active speaker one focus area and places responses beside the actual participant. It ties witnesses to the resolved session. Its C prototype ranks a small action tray from available actions. These are design and prototype mechanisms, distinct from measured adoption. [Visual direction](https://github.com/cenetex/app-ruby-high/blob/7ca0c5e9cdefaa2b67a9633c1bc60b3e575ac942/ruby2/visual-scene/VISUAL_LANGUAGE.md#L7), [action tray](https://github.com/cenetex/app-ruby-high/blob/7ca0c5e9cdefaa2b67a9633c1bc60b3e575ac942/ruby2/c/src/ruby2_ui.c#L783).

Signal gives named stations different lines at particular story steps. Grimoire describes the same economical pattern: a stable voice attached to a role and event. The Sector One companion similarly places the character over shared server state. These support a small cast with continuity. [Signal lines](https://github.com/cenetex/signal/blob/b8235a1236f7c5a391912b4e3225ef0eb659febd/shared/story_loop.c#L106), [voice guidance](https://github.com/atimics/grimoire/blob/b85e2e04c6fd2f47c2d498c10e453f78f3d0843b/EGREGOREGRAMMING_101.md#L145), [Sector One scope](https://github.com/cenetex/app-sector-one/blob/25c78f10daca075dc38469dcfddd7380418762a1/README.md#L5).

**RATi application:** Flash presents the lead view. One recurring countervoice can respond to what changed or what could weaken that view. Preserve its prior public position in the story so a later correction has meaning. The existing avatar abilities and newsroom proposal can supply these roles. Keep character identity in the report and reaction area of Detail.

After the basic reaction flow works in all markets, test two fixed purposes such as `What changed?` and `What could change this view?`. RATi supplies the words. The server accepts a closed set of intents. This is new action-contract work; the current empty-payload reaction remains the compatibility path.

A further historical source comes from CosyWorld's August 2025 breeding flow. It combined two characters' descriptions, personalities and memories into a new character brief, then introduced the result. For RATi, an accepted same-participant discovery supplies the event; a new avatar inherits recognisable traits from both originals. This gives the relationship a memorable face. Durable parent links, preserved factual attribution and correction handling belong to RATi's identity contract. [Historical breeding flow](https://github.com/cenetex/cosyworld/blob/ba3ca425d296a9f8bcc2b81a7f81626082ca9005/src/services/tools/tools/BreedTool.mjs#L82).

### 6. Keep the user's place through slow work

Swarm's design puts asynchronous results back into the conversation where the request began. Trebuchet's architecture separates shared action contracts from replaceable presentation. These are useful adjacent design patterns. Their settings, operational proof and terminal layouts serve their own products. [Swarm continuation](https://github.com/cenetex/swarm/blob/0db9fb402cf20c222537e04f513ef99c63332cd1/docs/design-philosophy.md#L175), [Trebuchet boundary](https://github.com/atimics/Trebuchet/blob/84061b1cfe58a1ac9b9b77e4cd11bcf3f1c7d52d/CLI_UX_GAP_ANALYSIS.md#L15).

**RATi application:** report and reaction jobs belong to a ticker story and account. Their results return to that story, survive reload and retain their access policy. The person can keep browsing while work completes. The shared Detail state owns the next action.

### 7. Measure the reason to return

CosyWorld distinguishes meaningful actions from presence and background work. Its return comparisons separate complete observation windows from pending ones. Those metrics are private product diagnostics. Ruby High's activation plan uses two small uncoached waves and asks what the player expected when they stopped. Both are useful methods; neither source establishes a retention lift for RATi. [Story measures](https://github.com/cenetex/cosyworld/blob/d76defb4e6644d5f6d0718906789da7ef211c839/v2/docs/story-metrics.md#L1), [activation method](https://github.com/cenetex/app-ruby-high/blob/7ca0c5e9cdefaa2b67a9633c1bc60b3e575ac942/docs/activation-playtest.md#L38).

**RATi application:** measure whether a user understands the story question, completes a chosen action and returns to its answer. Count an eligible return only after the result became available. Use explicit follows, Calls and result opens within RATi's privacy rules. Keep this measurement internal.

## The missing unit: a story within a ticker

A ticker can contain several stories. A quote alone has no ending. A report can become stale while its original question remains open. The next implementation needs a small story record tying those parts together:

- A stable identity and typed ticker subject.
- One plain question and opening time.
- The opening facts and report version.
- A revisit trigger, with a bounded review time as a fallback.
- Versioned public updates and corrections.
- A result or an explicit unresolved state.
- Optional explicit follow and Call relationships.

This is an internal structure. The screen says `Flash is watching whether buyers hold through the close`, followed later by the observed result. It keeps the internal identity and research workings private.

The story's review time and a Call's settlement rule are separate facts. Stocks retain their session-based settlement, coins retain their current close/expiry rules, and sports retains final-result settlement. The UI uses the same places and words for question, next update and Your Call. This preserves one design across different events.

For a coin story, a possible shared funding link can motivate a question about subsequent selling or liquidity. Describe the observed behavior and the remaining uncertainty. The story record preserves the original qualified claim when new information arrives.

## Revised three-screen design

**Ticker List** answers: what changed, and which story is worth opening? Keep one cue per row. A result for an explicit Call takes priority over a new public report. Your stories is a scope of the same List, driven by follows and Calls. Public discovery ordering stays shared. A first visitor can open a completed public story to understand the payoff before making a choice.

**Ticker Detail** answers: what is the question, what can I do, and what happened? Keep the price chart or score as the main visual. One Flash take names the question and next update. The report reader replaces the body in place. Call is the main active decision where available; Follow is a quiet way to keep the story. React opens its compact action. On return, the result occupies the main story area and the historical Call remains recognizable.

**Ticker Map** answers: which related story changes my understanding? A named shared connection links real subject relationships. One selected link gets one explanation. A connected ticker opens Detail. Map becomes useful through discovery, while its layout stays quiet.

The result view itself is the first keepsake: a chart or score, original choice, observed outcome and memorable response. It can later support a user-chosen export or share action. Use the existing visual and saved text for that artifact. A new image-generation pipeline adds little to this first slice.

## Revised delivery and validation

The six base findings remain the first repair group. The story contract can be designed alongside them. Then deliver the shared reports, coin support and reactions, followed by explicit follows, return cues and lasting result records. Character progression and matched competition follow evidence from the first complete loop.

The [backlog index](../playable-ticker-backlog.md) links the child issues, including the identity foundation. It puts the story contract in #245, follows and return cues in #242, lasting records in #243, the player study in #246, and the reaction-purpose experiment in #247. Individual issues own implementation scope and dependencies. This document owns the design decision. The newsroom proposal [#231](https://github.com/atimics/runner-watch/issues/231) can supply recurring reporters and assignments. Its stories should arrive through this shared surface; its operational controls retain their separate operator audience.

The first product study should use two small waves with clear stop points. Wave A tests the first visit and explanation of a result. Improve the earliest repeated confusion before Wave B. The return exercise includes both a material update and a resolved Call. Ask players to identify the original choice and explain what changed. Report raw counts and observed friction. The existing privacy model decides which aggregate events are available.

The three main hypotheses are:

1. A specific question and revisit time make the next visit easier to understand.
2. A single result with a recurring voice makes an outcome more memorable.
3. An explicit Follow action lets interested readers return without making a Call.

Success means a clear first visit and a useful return. The data will then tell us whether a character milestone or a matched Flash duel improves that experience.

## Source snapshots

| Project | Local committed revision | Evidence used |
| --- | --- | --- |
| Ruby High | `7ca0c5e9cdef` | Class design, result persistence tests, graduation record, C action tray, playtest method |
| CosyWorld | `d76defb4e664` | Public-state tests, daily Journal code, story metrics, player vocabulary and design drift |
| Signal | `b8235a1236f7` | Ordered story state, prerequisite tests, progress persistence and attention policy |
| Crownless | `e5251dc6d582` | Implemented interaction flow, quoted commitment, durable history and bounded expansion plan |
| GLM Roguelike | `74a87487cf6b` | Direct action, map change, resource consequence and a clear terminal goal |
| Sector One companion | `25c78f10daca` | Character client over shared server state; documented integration boundary |
| Swarm | `0db9fb402cf20` | Context-preserving asynchronous continuation design |
| Trebuchet | `84061b1cfe58` | Shared action contracts and normalized confirmation design |
| Grimoire | `b85e2e04c6fd` | Stable voice attached to a role and event |

The shared `~/develop/UX_GUIDE.md` helped locate these patterns. Its older Runner Watch advice includes visible models and source links. RATi's current user direction governs here: the public screen contains useful product facts and generated authorship; internal source, provider and diagnostic information remains in the service layer. Signal's low-clarity text styling is also specific to its game. RATi should keep all essential text readable while stating uncertainty plainly.
