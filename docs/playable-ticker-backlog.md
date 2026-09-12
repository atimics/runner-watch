# Playable ticker backlog

Groomed 12 September 2026. The live parent is [#232](https://github.com/atimics/runner-watch/issues/232). Issue bodies own current scope, status, dependencies and acceptance checks. This page records the delivery structure created from PR #229's review, the first design and the [cross-project assessment](research/game-pattern-assessment.md).

## Repair the base

| Issue | Work | Dependency |
| --- | --- | --- |
| [#233](https://github.com/atimics/runner-watch/issues/233) | Refresh quote, Call result and available actions together | Shared base in #229 |
| [#234](https://github.com/atimics/runner-watch/issues/234) | Distinguish current and paused coin prices | Shared base in #229 |
| [#235](https://github.com/atimics/runner-watch/issues/235) | Keep pending sports scores truthful | Shared base in #229 |
| [#236](https://github.com/atimics/runner-watch/issues/236) | Preserve Call choice, entry, terms and reward | #233 |
| [#237](https://github.com/atimics/runner-watch/issues/237) | Explain meaningful Map connections | Shared base in #229 |
| [#238](https://github.com/atimics/runner-watch/issues/238) | Show chart magnitude and period | Shared base in #229 |

These are the six P2 findings from the [independent base review](reviews/pr-229-base-design.md).

## Establish pseudonymous identity

The [identity design](research/pseudonymous-identity-design.md) makes pseudonyms the shared cast. Public associations, accepted aliases and later corrections become story updates. Same-participant, shared control and possible links retain distinct meanings.

| Issue | Work | Dependency |
| --- | --- | --- |
| [#248](https://github.com/atimics/runner-watch/issues/248) | Stable avatars, ancestry, entity references, dated claims and legacy migration | Define alongside #245 and the base repairs |
| [#249](https://github.com/atimics/runner-watch/issues/249) | Add Helius wallet attribution with caching and shared budget limits | #248 and the existing Helius budget ledger |
| [#250](https://github.com/atimics/runner-watch/issues/250) | Combined-avatar reveals, reversible identity grouping and shared Map treatment | #248, #245 and the base Map in #237; #249 supplies wallet claims |

Identity grouping preserves earlier aliases, reports, authors and account ownership. The proposed attribution sublimit is 1,000 credits within the existing 10,000-credit daily Helius ceiling. Source evidence and internal attribution details stay in the service layer.

An accepted combination creates a new fictional avatar with recognisable traits from both originals. CosyWorld's old breeding flow provides the creative reference. Parent links preserve ancestry, and corrections restore valid earlier characters. The reveal uses the existing Detail and Map; #246 tests whether readers recognise the inherited traits and understand the discovery.

## Complete the shared story

| Issue | Work | Dependency |
| --- | --- | --- |
| [#245](https://github.com/atimics/runner-watch/issues/245) | Define story question, revisit trigger, versions and outcome | Define alongside the base repairs and #248; retain identity revisions |
| [#240](https://github.com/atimics/runner-watch/issues/240) | Add coin report subjects | #245 |
| [#239](https://github.com/atimics/runner-watch/issues/239) | Read and commission reports in shared Detail | #240, #245 |
| [#241](https://github.com/atimics/runner-watch/issues/241) | Restore durable avatar reactions across markets | #240 |
| [#242](https://github.com/atimics/runner-watch/issues/242) | Follow stories and review outcomes in List and Detail | #236, #239, #245 |
| [#243](https://github.com/atimics/runner-watch/issues/243) | Keep resolved stories as lasting records | #242, #245; identity-reveal extension uses #250 |

The existing newsroom proposal [#231](https://github.com/atimics/runner-watch/issues/231) can supply recurring voices, reports and updates through this same story contract. Its operator work keeps its own audience.

## Learn before expanding progression

| Issue | Work | Dependency |
| --- | --- | --- |
| [#246](https://github.com/atimics/runner-watch/issues/246) | Observe the first visit and return journey | #236, #239, #242; reveal study follows #250 |
| [#247](https://github.com/atimics/runner-watch/issues/247) | Test two useful purposes for a reaction | #241, #245; informed by #246 |
| [#244](https://github.com/atimics/runner-watch/issues/244) | Define fair matched player-versus-Flash Calls | #242; product decision after #246 |

Cosmetic milestones remain an experiment under #243. The first release makes the story question, choice and result clear across Stocks, Memecoins and Sports. Each issue carries focused validation for the actual routes and preserves the public-data boundary.
