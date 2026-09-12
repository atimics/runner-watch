# Market Actors: insider map and AI characters

Status: v1 implemented 2026-09-12. Design agreed 2026-09-12.

v1 ships the stock insider map and memecoin wallet-cluster map, deterministic
pseudonyms, on-demand AI portraits cached through OpenRouter, and on-demand
third-person character comments. Deferred: background event-driven comments,
and reading clusters from `memecoin_chain_events` directly instead of the saved
forensics snapshot.

## Goal

Add a second surface to the board that maps **who is moving a market**, and
give each mover an AI character that can post evidence-bound comments.

- **Stocks**: the movers are SEC insiders (Form 4 filers, SC 13D/G owners).
- **Memecoins**: the movers are on-chain wallet clusters ("insiders" for coins).

The map is Bubblemaps-style: characters and markets are bubbles, relationships
are links, and clicking a character opens their avatar, evidence, and comments.

## Non-goals

- No claim that an AI character **is** the real person.
- No real-person likeness in generated avatars.
- No trading advice, and no invented facts.

## The Actor primitive

Everything hangs off one concept because stocks and memecoins are the same shape.

| | Stock | Memecoin |
|---|---|---|
| Actor | Form 4 / 13D/G filer | Wallet cluster |
| Tie | actor -buy/sell/owns-> ticker | actor -funded/bought/withdrew-> token |
| Evidence | `sec_filings.accession` | `memecoin_chain_events.signature` |
| Identity | real, public, but represented by a fictional character | pseudonymous, no identity claim |

Proposed tables (migration 66+):

### `market_actors`

| column | notes |
|---|---|
| `id` | text primary key |
| `kind` | `person` or `cluster` |
| `domain` | `stock` or `coin` |
| `stable_key` | unique; `sha256("insider:"+cik+":"+normalized_name)` or cluster root wallet hash |
| `user_id` | synthetic system user, the bridge to the comment rails |
| `display_name` | fictional handle, never the real name |
| `ability_id` | persona, reuses `COMMENT_AVATAR_ABILITIES` |
| `avatar_seed` | deterministic seed for the face and portrait |
| `portrait_ref` | cached image reference, empty until generated |
| `created_at`, `updated_at` | |

### `actor_ties`

| column | notes |
|---|---|
| `id` | text primary key |
| `actor_id` | -> `market_actors.id` |
| `subject_kind` | `stock` or `coin` |
| `subject_key` | ticker or coin id |
| `role` | `officer`, `director`, `ten_percent_owner`, `creator`, `funder`, `cluster_member` |
| `direction` | `buy`, `sell`, `hold` |
| `weight` | transaction value or cluster flow |
| `as_of` | event time |
| `evidence_kind` | `sec_filing` or `chain_event` |
| `evidence_id` | accession or signature |

Indexed on `(subject_kind, subject_key, as_of)`, `(actor_id, as_of)`, and
`(evidence_kind, evidence_id)`.

### `actor_cluster_members`

`actor_id` -> wallet, with the evidence edge that joined the cluster. Only for
`kind='cluster'`.

## Stock insiders

Derivation reads `sec_filings` where `form` is `4`, `SC 13D`, `SC 13G`.
`actor`, `actor_title`, `reporting_person_types`, `transaction_value`,
`stake_change_pct`, `beneficial_ownership_pct`, and `beneficial_owner_names`
are already stored, so no new ingestion is needed.

Pseudonymization rules:

- A real name is **never rendered on the character**. The character has a
  fictional handle and face.
- The public tie exposes a **role bucket** only: "an officer", "a director",
  "a 10% owner".
- The underlying filing stays one click away as evidence. The real name is
  visible there because the filing is public, but it is clearly the source
  document, not the character speaking.
- `stable_key` is a hash, so the same filer always maps to the same character
  without the public surface exposing the mapping.

Pseudonyms are derived deterministically (digest of `stable_key`) using the
existing adjective/animal generators in `pseudonyms.py`, not `secrets`. That
keeps a character stable across restarts and renders.

## AI-generated portraits

OpenRouter already hosts image models (`google/gemini-3.1-flash-image`,
`openai/gpt-5-image-mini`, and others). Portraits are generated through the same
OpenRouter key the rest of the app uses, via chat completions with
`modalities: ["image", "text"]`.

Rules:

- One portrait per actor, generated **lazily on first view**, then cached.
- The prompt asks for a fictional mascot character and explicitly forbids real
  resemblance, real logos, and text.
- Cache in the database (`market_actors.portrait_ref` plus a stored blob or
  object store key) and serve through a cacheable route.
- If generation fails or the budget is exhausted, fall back to the existing
  procedural face from `comment_avatar_profile`. The map must never block on an
  image.
- Cost guardrail: a global daily portrait budget, same idea as the Flash wallet.

This mirrors the Dash pattern (`web/static/dash-cheetah.png` +
`AVATAR_PORTRAITS`), generalized to dynamic actors served from storage.

## Memecoin clusters

`memecoin_forensics.analyze_events()` already returns `wallet_links` and
findings (`common_funder`, `synchronized_buys`, `repeated_round_trips`,
`creator_*`). Today that output is only saved as a blob in
`worker_state['memecoin_forensics']`.

The derivation job will:

1. Build connected components from `wallet_links` (plus `common_funder` and
   `synchronized_buys` related wallets).
2. Create a `cluster` actor per component above a minimum size.
3. Write `actor_ties` per member token and `actor_cluster_members` per wallet.
4. Keep every tie evidence-linked to a `memecoin_chain_events.signature`.

Clusters are observations, not ownership claims. The existing copy in
`memecoin_forensics` ("shared services can also create this funding pattern")
stays attached to the tie.

## Map view

Add `map` to `BOARD_VIEWS` in `main.py`, so the board stays one screen with a
query-param view, consistent with `pulse` / `changed` / `calls`.

- Packed bubble layout grouped by market, sized by weight, colored by cluster.
- Links drawn from `actor_ties`, thickness by weight.
- Tap a bubble -> shared actor panel: portrait, fictional handle, role bucket,
  evidence list, and comment thread.
- Reduced-motion and small screens fall back to a sorted list of actors.
- The panel is one component shared by stocks and memecoins.

## AI characters that comment

Reuse `_generate_comment_from_evidence` and the `ticker_comments` rails
(`subject_kind` / `subject_key` / `source='ai_avatar'` / `generation_model`).
An actor is a synthetic user with a `comment_avatars` row, exactly like Dash.

- **Third person, evidence-bound.** The character talks about the filing or the
  cluster move, never as the real person. Example voice: "The officer filed a
  $2.1M open-market buy, his first since March."
- **Event-driven only**: new Form 4, stake change, cluster buy/sell, liquidity
  withdrawal. No idle chatter.
- **Budgeted**: per-actor daily cap plus a global ceiling, funded like Dash
  through the Flash wallet. A comment that fails the evidence contract is not
  posted.
- Every comment shows the evidence link and the standing label
  "AI character - built from public data".

## Guardrails

- Fictional identity is explicit on every surface: handle, label, and an
  "about this character" note.
- No real-person likeness in prompts or generated images.
- Cluster findings keep their "observation, not identity" disclaimer.
- Characters are hidden from `privacy.py` account exports as system users.
- `legal_risk.py` review flow applies if a character is reported.

## Phasing

1. **Persistence** - `market_actors`, `actor_ties`, `actor_cluster_members`;
   derive stocks from `sec_filings` and coins from `analyze_events`. Read-only.
2. **Map view** - `?view=map`, bubbles, links, actor panel (no AI, procedural
   faces).
3. **Portraits** - OpenRouter image generation, cache, budget, fallback.
4. **Comments** - third-person, event-driven, budgeted. Stocks first, then coins.
5. **Polish** - cluster quality thresholds, mobile layout, privacy export.

## Open decisions (defaults proposed)

1. **Insider scope**: Form 4 + SC 13D/G first; congressional disclosures
   (`congressional_disclosures.py`) later. _(default)_
2. **Map placement**: add `map`, keep `pulse` / `changed` / `calls`. _(default)_
3. **Budget**: per-actor 3 comments/day, global 200 portraits/day and 300
   comments/day. _(default; tune before launch)_
4. **Cluster floor**: at least 3 wallets and 2 evidence edges before a cluster
   becomes an actor. _(default)_
