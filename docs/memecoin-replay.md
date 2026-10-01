# Memecoin replay

The details page shows a replay of a new memecoin's launch, with a GIF and a
verifiable evidence package. The Telegram channel no longer posts these GIFs
("New coin detected"): they were removed as noise. The channel now hears about
memecoins through transition alerts (docs/telegram-channel.md). The
`memecoin_replay_posts` table stays for old rows, and nothing writes to it.

Replay records provide a stable starting point for a later Flash monitoring
flow. Pricing and additional collection budgets belong to that integration.

## Details page and evidence

The details page starts at the token launch and grows a bubble map through saved
slots. Replay supports a loop, scrubbing, keyboard selection, reduced motion,
and GIF download. A channel replay link pins its exact saved revision. The
receipt links continue to work after the ingestion tables reach their retention
limit because the package contains the selected source receipts.

Launch facts come from the existing Pump parser. A case with a pool or trades
and an open launch requirement keeps the launch label pending. Wallets link to
tokens they traded. Shared holdings require positive balances for both tokens
under one owner in the same transaction's post-state. Amounts retain raw integer
strings, decimal counts, and mint addresses. These are dated balance observations.
Executable liquidity and real-world ownership remain separate evidence questions.

The worker uses the first and latest 64 token event rows and up to 64 context
rows across 16 observed wallets. A package holds at most 128 receipts, 128 events,
4 MiB of receipts, 48 drawing nodes, and 12 keyframes. Coverage reports the
selection counts and recent program gaps. Collection reads the existing receipt
database. Helius ingestion keeps its existing request budget.

The renderer makes an 800 × 600 GIF with 800 ms eased transitions, a 950 ms launch
hold, 450 ms intermediate holds, a 1,700 ms final hold, and a 900 ms return. The
saved geometry and timing drive both the page and GIF. GIF size is capped at
8 MiB. Each token can retain 16 immutable replay revisions under this initial
archive budget. At capacity, the saved replay stays available and the case
records `archive_capacity` for review. The next monitoring policy can set a
larger archive budget. Receipt and package hashes identify the saved content;
the RPC source supplies chain observations.

## Quality contract

The policy is `repository-qc1000/memecoin-replay/1`, owned by this repository.
Its scope is machine evidence and publication quality. People review the output.

| QC component | Objective and control | Evidence |
| --- | --- | --- |
| Governance | Repository owns the versioned scope and limits | Policy and source digest in each package |
| Ethics | Claims fit observed evidence and preserve open launch status | Pending-launch and subject-scope tests |
| Acceptance | Work starts from a known saved Solana mint | Subject checks and bounded database reads |
| Performance | Rebuild events and links from successful transaction receipts | Decoder replay and tamper tests |
| Resources | Bound events, nodes, frames, GIF bytes, and archive revisions | Visible counts and limit tests |
| Communication | Page, package, and GIF point to one revision | Pinned routes and retained receipt tests |
| Monitoring | Record actual failures, retries, and delivery uncertainty | Lease, cancellation, and outcome tests |
| Evaluation | Run the behavior checks on the reviewed source | CI test results and quality artifact |

Runtime verification checks the receipt hashes, decoded event window, source
links, subject, chronology, geometry, policy, package hash, and receipt Merkle
root. A failed check retains the evidence and records a short failure code.
Rendering runs under a database lease. A temporary render failure retries from
the saved evidence. 
## Team integration

The replay worker renders saved evidence, then waits 15 seconds before its next
cycle. The worker is included in the required process heartbeat.

Validation uses the existing Python/Pillow stack and the repository's browser
test suite.
