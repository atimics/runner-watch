# Pseudonyms, public identities and story reveals

Design revision, 12 September 2026. Part of [PR #230](https://github.com/atimics/runner-watch/pull/230) and [backlog #232](https://github.com/atimics/runner-watch/issues/232). This extends the [game-pattern assessment](game-pattern-assessment.md). The work below is planned.

**A pseudonym is the face a reader remembers. Our understanding of the participant behind it can change.** This applies to Stocks, Memecoins and Sports. A discovery can connect two familiar characters, associate one with a public organization, or correct an earlier connection. When two avatars resolve to the same participant, a new combined avatar gives that discovery a lasting face. The original characters remain part of its history.

## Five separate things

| Thing | Meaning | Example |
| --- | --- | --- |
| Avatar | A lasting fictional face, handle and voice | Copper Lens |
| Reference | An observed wallet, filing identity or league identity | A Solana address or a reporting person's SEC identifier |
| Entity | The person, organization or other participant we describe | A company, athlete or fund |
| Claim | A dated statement about identity or a relationship | An organization operated a wallet during a stated period |
| Resolution event | A recorded change to our understanding | Two earlier participants are accepted as aliases of one entity |

Give each record an opaque ID that stays fixed. Names, portraits, wallet membership and source labels can change around those IDs. Store chain and network with an address, and the issuing system with a filing or league identifier. Where an identifier is missing, keep a separate unresolved reference with its context.

A market avatar can represent a person, an organization or a provisional wallet group. A group may later resolve into several entities. Several avatars may turn out to refer to one entity. Support both directions from the start.

Keep three avatar roles clear: the player's chosen identity, the AI reporter who writes, and the market character being discussed. The reporter has an author record. The market character has observation links. The player has account ownership and access rights. Each keeps its own history.

## Three kinds of connection

| Finding | Meaning | Shared Map treatment |
| --- | --- | --- |
| Same participant | Two records describe one entity | One current entity node with a new combined avatar; both original faces remain in its history |
| Shared operator | Separate participants have a supported control relationship | Separate nodes joined by `Shared operator` |
| Possible link | Evidence suggests a relationship that remains open | Separate nodes; a qualified connection appears when useful to the story |

Ownership, employment, sponsorship, custody, funding and identity each get a distinct relationship type. A sports club and its owner are separate entities joined by ownership. An executive and the company are joined by a role. An exchange wallet describes the service's custody role; its customers retain their separate identities.

Common funding, synchronized trading and similar transaction sizes produce research leads. Accepting a same-participant claim requires evidence about identity. Accepting shared control requires evidence about control. A shared exchange withdrawal source supports a funding observation. The acceptance rule records this difference.

## Public wallet attribution

Helius offers single and batch Solana wallet identity lookups on paid plans. The batch accepts up to 100 addresses and returns known names, categories and tags. Its Wallet API is currently in beta. Treat each result as a dated provider claim for review. [Helius identity guide](https://www.helius.dev/docs/wallet-api/identity).

Public organizations also publish their own addresses. Binance's reserve page offers an address download and describes funds held in custody for customers. This is an example of a first-party organizational source. [Binance reserve page](https://www.binance.com/en/proof-of-reserves).

Public labels can be corrected. Etherscan's stated policy uses an individual's public declaration of address ownership for an individual name tag, and provides a correction/removal process. This is a useful publication rule for RATi's wallet-to-person links. [Etherscan name-tag policy](https://info.etherscan.com/public-name-tags-labels/).

Start with official organizational disclosures, public personal declarations and authoritative market records. Record their exact scope and dates. Import provider labels as attributed claims and review their support where available. Agreement copied from one origin counts as one source. Behavioral links remain hypotheses with reasons and a review trigger.

A signed wallet challenge establishes key control at that time. Linking control to a named entity needs a supported public association. Operators, signers, custodians and beneficial owners retain distinct roles. Name-service ownership also has a time period.

Public attribution is a separate publication decision from entity matching. Keep the fictional handle and face as the main identity. A useful accepted association can appear below it, such as `Operated by Aster Labs`. For wallet-to-person names, begin with public self-disclosures. Account information follows existing account access and privacy rules. Chain activity continues to determine memecoin discovery; labels enrich participants in a discovered story.

### Helius budget

The current price is 100 credits for a single identity request and 100 credits for a batch of up to 100 addresses. [Helius credit schedule](https://www.helius.dev/docs/billing/credits).

Reuse the shared 10,000-credit daily ceiling in `memecoin_evidence.reserve_credits`. Propose an identity sublimit of 1,000 credits within that ceiling: at most ten batch attempts a day at the verified price. Ten full successful batches could refresh up to 1,000 addresses. Cache hits, partial batches, retries and other ingestion work determine actual coverage.

Reserve credit before every attempt, including retries. Enforce both limits together across workers. Reuse cached labels, cache unknown results briefly, and prioritize material story participants. Background work supplies saved results to browsing. Store response time, expiry and schema version. Use the API-key header and redact credentials from failures. Recheck pricing before enabling the adapter. Account access and real responses still need a bounded live check during implementation.

## Time, grouping and corrections

Each claim stores its subject, relationship, object, evidence references, origin, review decision and superseded claim. Keep two clocks: the period the statement describes, and when RATi learned it. An unknown start date stays unknown. Today's attribution then retains its actual historical scope.

Use proposed, accepted, disputed and retracted states. A deterministic acceptance policy can handle well-supported cases; unresolved conflicts stay in the research queue. Record the policy version and reason internally. Public wording follows the accepted relationship and its uncertainty.

An accepted same-participant claim creates a reversible grouping of existing records and a new combined avatar. Preserve original IDs, handles, portraits, observations and report versions. Give the combined avatar its own persistent ID and name, attached to that group revision. Older links retain their original avatar context and show the current relationship. A later correction can split the group and restore the earlier identities.

Apply equivalence only to compatible entity types supported by active same-participant claims. Other relationship edges keep their own meaning. On retraction, rebuild the affected group from its remaining accepted claims. Version the resulting view so caches and readers use the same revision. An expiring control relationship preserves its historical period; a mistaken identity requires retraction and correction.

Original reports retain the alias, claims, author and knowledge available when published. New findings append a dated update. Corrections appear beside affected assertions in an older report. The service keeps the full audit trail.

## A reveal is a story event

Fictional example: Copper Lens appears in a stock story. Glass Fox appears in a coin story. Both are later found to represent the same fund. The update says: `Copper Lens and Glass Fox represent the same fund.` If only common management is established, it says: `Copper Lens and Glass Fox share an operator.`

This gives the reader a reason to return: familiar characters now have a meaningful connection. A public name can arrive later. Resolving two aliases already has value while the entity stays pseudonymous.

- **Ticker List:** one material cue, such as `Two characters are connected`, in the existing story position. The shared priority rule chooses between this update and the player's Call result.
- **Ticker Detail:** one reveal presents the two familiar faces and their combined avatar, with the precise relationship and date. Opening the update uses the existing report body. A quiet `Formed from` line preserves recognition after grouping.
- **Ticker Map:** accepted aliases share one current entity node with the combined avatar. Shared operators and possible links use separate nodes. A selected link gets one plain explanation. Related tickers open the usual Detail.

Use the same components and transitions in every market. Identity context stays inside the story or selected Map area. Keep source receipts, provider tags, confidence scores, wallet dumps and review controls in the service layer. The public presenter selects useful story facts, including a meaningful correction or uncertainty.

## Combined avatars: the CosyWorld breeding pattern

The old CosyWorld `BreedTool` took two characters' descriptions, personalities and memories, asked for a combined character and backstory, and passed the result into character creation. The creation flow then gave the new character an image and introduction. This historical implementation is visible at commit `ba3ca425d296a9f8bcc2b81a7f81626082ca9005`, dated 7 August 2025. [Combination prompt](https://github.com/cenetex/cosyworld/blob/ba3ca425d296a9f8bcc2b81a7f81626082ca9005/src/services/tools/tools/BreedTool.mjs#L82), [creation and introduction](https://github.com/cenetex/cosyworld/blob/ba3ca425d296a9f8bcc2b81a7f81626082ca9005/src/services/tools/tools/SummonTool.mjs#L175).

RATi can use the same creative pattern for an accepted identity discovery. **Two familiar characters become a new character with visible ancestry.** The avatar represents our newly combined understanding of one existing participant. The real-world participant keeps its own identity and history.

Fictional example: **Copper Lens + Glass Fox → Copper Fox.** The result carries Copper Lens's round copper glasses and Glass Fox's ears and pale markings. It has one coherent face, its own name and a short voice description. Its introduction says: `Two trails led to the same fund. Meet Copper Fox.`

### What the new character inherits

| Part | Rule |
| --- | --- |
| Appearance | Carry one clear visual feature from each original into a shared art style. Save the selected traits and portrait version. |
| Name | Create a distinct fictional name with a hint of the originals. Save it once and handle display-name collisions through the stable ID. |
| Voice | Combine a few saved style traits, such as patience and curiosity. Keep the resulting voice short and recognisable. |
| Story memory | Link the originals' public story events and preserve who said what. Approved facts, times and corrections remain attached to each event. |
| Ancestry | Store both parent avatar IDs, the resolution event, creation time and the current status. The public view shows `Formed from Copper Lens and Glass Fox`. |

Creative generation receives a small brief of approved visual traits, voice traits and public story facts. Private reports, account memories and internal research stay within their existing access rules. Factual claims and measured performance keep their evidence and original author; appearance and voice provide the creative freedom.

The old tool passed parent IDs to `SummonTool`, while the inspected creation path built its saved avatar from the prompt and channel. RATi's parent links therefore need explicit persistence and recovery tests. Store the lineage and creation event together so restarts preserve the result. The source review establishes the old creative flow; the new design defines durable ancestry.

### One reveal, then a lasting character

Publish the combined avatar after the same-participant decision is accepted. Shared operators and possible links retain their relationship treatment. The reader opens the story update to see the reveal. Their follow then continues through that story with its new face.

In Detail, two small original portraits lead into one main combined portrait and one sentence about what changed. Use a brief transition on the first explicit open, with a static view for reduced motion. Reopening shows the settled character. Parent faces remain available through a compact ancestry view inside the same Detail or selected Map area. The shape of this moment stays the same across all three markets.

Key creation to the accepted resolution event and its parent records. Repeated ingestion, reverse parent order, refreshes and job retries reuse that result. When an existing combination joins another participant, record the two current parent avatars and their original ancestors. Routine wallet membership changes retain the current character.

Corrections preserve the lineage record and mark the affected combination as superseded. Restore valid prior avatars where possible. If the corrected group is new, create its own combined avatar under a new accepted event. Historical reports keep the faces and authors present at publication, with their current correction status beside them. Reaccepting the exact same supported group can reuse its saved portrait; the correction and later acceptance retain separate dates.

A saved procedural portrait can supply the first reveal. Any later generated portrait uses a separate bounded media job and the existing approved media allowance. Store one result per brief version, reserve cost before each attempt, and reuse it across stories and markets. Media completion can refine the saved portrait while the identity update is already readable. Helius credits remain assigned to chain and identity data.

## Reporters and interviews

The newsroom in [#231](https://github.com/atimics/runner-watch/issues/231) can investigate an identity question. Assignments reference existing entities and claims. A reporter may propose a link; the accepted evidence process decides its status before a reveal is published.

Preserve who wrote each report and spoke each generated line. Market avatars supply a fictional perspective grounded in available evidence. Public copy identifies generated dialogue as an AI interpretation. A supported external quote has its own real speaker attribution. Several avatars repeating one source contribute one source of support.

Identity resolution, public naming and editorial publication each have a recorded decision. Paid actions follow their current report and reaction rules. Accepted public facts use the story's publication policy.

## Migration and accounting

Current source at PR #229 exposes two gaps:

- Stock actors use only the normalized name for their key in [`market_actors.py`](../../src/runner_web/market_actors.py#L264). Two people with that name can share an actor. Use the reporting person's own identifier where available, with issuer and role stored separately. Separate ambiguous legacy records using the underlying filings.
- Coin actors derive their key from the finding family and sorted wallet set in [`market_actors.py`](../../src/runner_web/market_actors.py#L372). Membership changes can create another actor. Store membership as dated observations attached to persistent IDs. Reconcile earlier groups through claims.

Preserve existing handles and deep links during migration. An ambiguous old record can resolve to a group with a correction until its observations have been assigned. Keep person, organization, group and account-reference types explicit. Use league identifiers for sports participants. Record transfers, sponsors and team ownership as dated relationships.

The actor service uses synthetic users for avatars and comments. Keep account IDs, ownership, access, Flash balances and Call settlement in their existing account system. Entity resolution updates the research view and story links. Account export and deletion continue to operate on account-owned records.

Deduplicate economic events before computing entity totals. On Solana, use chain, network, transaction and instruction identity with the asset and action. Retain participant roles on that event. Keep value, token amount and ownership percentage as separate units. One event can involve several participants while contributing once to the relevant total. Grouping aliases also deduplicates update delivery for a followed story. A split rebuilds totals and cues consistently.

## Delivery and proof

The backlog adds three slices: [stable identity and claim records #248](https://github.com/atimics/runner-watch/issues/248), including avatar ancestry; [budgeted Helius attribution #249](https://github.com/atimics/runner-watch/issues/249); and [reversible resolution and combined-avatar reveals #250](https://github.com/atimics/runner-watch/issues/250). Reports store their identity revision. The Map, lasting record, newsroom and return study use the same contract.

Implementation fixtures must cover:

- Two stock insiders with the same name; one person under two names; a growing wallet group; the same address text in different networks.
- Accepted aliases, shared control and a shared exchange funder, each with its proper relationship.
- Corrected or removed public labels; changing operators; unknown historical control periods.
- An A–B–C identity chain followed by retraction; conflicting concurrent decisions; recovery after interruption.
- One economic event reached through two aliases, with correct totals after grouping and splitting.
- An old report opened after correction, with original authorship and current status visible.
- A followed story spanning a reveal, one update cue, and preserved Call terms and account balances.
- A combined avatar with a recognisable trait from each parent, persisted ancestry and one identity across retries, reversed parent order and concurrent jobs.
- A combined avatar joining another avatar; a partial split; restoration and later reacceptance, with correct historical faces and authors.
- A delayed or failed portrait job, a cached procedural fallback and a bounded approved media allowance; private parent context remains within its access rules.
- Budget exhaustion, retry charges, cached unknowns and changed provider responses.
- Reveal and correction journeys in all three markets, with keyboard access, small screens and reduced motion. Public responses contain only selected story facts.

Validation for this design revision is source and document review. Runtime tests and a bounded provider check belong to implementation.
