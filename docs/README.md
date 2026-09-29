# Documentation index

This directory mixes living design documents with dated planning records. Nothing
here is generated; update the owning document when behavior changes. Start with
the root [README](../README.md) for how the service is built and run.

## Current behavior

| Document | Covers |
| --- | --- |
| [market-screens/README.md](market-screens/README.md) | The shared List / Detail / Map screen contract for all three markets |
| [scoring-contracts.md](scoring-contracts.md) | Attention units, forecasts, eligibility, label integrity and purged evaluation |
| [stock-scanner.md](stock-scanner.md) | Desktop scanner controls and what each filter means |
| [memecoins.md](memecoins.md) | Solana discovery, Helius budget and collection windows |
| [memecoin-forensics.md](memecoin-forensics.md) | On-chain forensic evidence methods |
| [memecoin-replay.md](memecoin-replay.md) | Replay rendering and channel GIF delivery |
| [ticker-maps.md](ticker-maps.md) | Per-ticker map placement and navigation |
| [market-actors.md](market-actors.md) | Insider/cluster map and AI characters (v1 record) |
| [product.md](product.md) | What the product is: Calls, Comments, Reports, and what is not planned |

## Direction and in-progress work

These describe planned or partially implemented work. Treat the linked issue or
pull request as the source of truth for current status.

| Document | Status |
| --- | --- |
| [market-list-ux.md](market-list-ux.md) | Redesign: tight tagged list, one composite score, top-bar breadth filters, ticker = chart → map → metrics → comments, Avatar Sentiment ([preview](market-list-preview.html)) |
| [research/market-attention-study.md](research/market-attention-study.md) | Market attention research: ML papers, frozen candidate, chronological results, and replay receipts |
| [research/attention-shadow-release.md](research/attention-shadow-release.md) | Live attention trial, durable outcomes, operations, and release gates |

## Historical records

The screenshots under `market-screens/` are snapshots captured on 12 September
2026 from local previews with sample data. They document a point in time and are
not kept in sync with the code.
- [Performance hot paths](performance-hot-paths.md): bounded queue reads, indexed history and benchmark receipts.
