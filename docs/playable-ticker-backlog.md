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

## Complete the shared story

| Issue | Work | Dependency |
| --- | --- | --- |
| [#245](https://github.com/atimics/runner-watch/issues/245) | Define story question, revisit trigger, versions and outcome | Can be designed alongside the base repairs |
| [#240](https://github.com/atimics/runner-watch/issues/240) | Add coin report subjects | #245 |
| [#239](https://github.com/atimics/runner-watch/issues/239) | Read and commission reports in shared Detail | #240, #245 |
| [#241](https://github.com/atimics/runner-watch/issues/241) | Restore durable avatar reactions across markets | #240 |
| [#242](https://github.com/atimics/runner-watch/issues/242) | Follow stories and review outcomes in List and Detail | #236, #239, #245 |
| [#243](https://github.com/atimics/runner-watch/issues/243) | Keep resolved stories as lasting records | #242, #245 |

The existing newsroom proposal [#231](https://github.com/atimics/runner-watch/issues/231) can supply recurring voices, reports and updates through this same story contract. Its operator work keeps its own audience.

## Learn before expanding progression

| Issue | Work | Dependency |
| --- | --- | --- |
| [#246](https://github.com/atimics/runner-watch/issues/246) | Observe the first visit and return journey | #236, #239, #242 |
| [#247](https://github.com/atimics/runner-watch/issues/247) | Test two useful purposes for a reaction | #241, #245; informed by #246 |
| [#244](https://github.com/atimics/runner-watch/issues/244) | Define fair matched player-versus-Flash Calls | #242; product decision after #246 |

Cosmetic milestones remain an experiment under #243. The first release makes the story question, choice and result clear across Stocks, Memecoins and Sports. Each issue carries focused validation for the actual routes and preserves the public-data boundary.
