# A market-based attention score

Study date: 2026-09-24 UTC. Baseline: `main` at `2a82810`.

**Recommendation: learn the chance of a material move from volatility, price
movement, volume, and the other stocks in the current scan.** Use a symmetric
event target: a move of at least 4% in either direction during the next hour.

The candidate found **172 observed event selections in 340 review slots**, against
**143** for the current market score. That is **20.3% more observed hits** on four
later trading dates. This supports a prospective shadow trial. Wider market
coverage and calibration are the next evidence gates for adoption.

## What the current score measures

`runner_web.attention.market_activity` supplies up to 80 market points:

```
volume   = min(35, 8 * log2(max(1, session relative volume, recent relative volume)))
momentum = min(30, max(6 * abs(5-minute move), 2 * abs(15-minute move)))
move     = min(15, abs(session price change))
market   = (volume + momentum + move) * quote freshness
```

The final attention score adds capped filing, news, external social, and cluster
holding points. A trading halt has a separate urgent flag. Risk and permission
to act have their own outputs. The public endpoint observed during this study
reported policy v2; the repository had v3 with the extra cluster contribution.
Both use the same market formula evaluated here.

The scanner already compares volume with the same time of day in prior sessions.
The useful next change is to learn how the signals combine. A 2% price change
gets the same movement points across stocks with very different volatility.
The formula also gives high session movement points after an earlier jump has
settled. The candidate can use recent volatility, volume, and other stocks to
judge the chance of another material move.

## Lessons from ML papers

| Paper | Relevant result or method | Use in this study |
| --- | --- | --- |
| [MASTER, Li et al., AAAI 2024](https://arxiv.org/html/2312.15235v1) | Uses market conditions to select features and models changing relations between stocks. Its experiments use daily Chinese equities and a five-day target. | Compare stock features with the median move, volatility, and volume in the same scan. This is a small, testable use of market context. |
| [Temporal Relational Ranking, Feng et al., 2019](https://arxiv.org/abs/1809.09441) | Frames stock selection as a ranking task and includes changing stock relations. | Judge the top ten review slots in each scan, which matches the attention task. |
| [Temporal Fusion Transformers, Lim et al., 2021](https://arxiv.org/html/1912.09363v3) | Combines feature selection, temporal patterns, and multi-horizon forecasts. Its volatility analysis shows different attention patterns across regimes. | Keep time, quote age, volatility, and changing market context explicit. Reserve a larger sequence model for a larger dataset. |
| [Grinsztajn et al., NeurIPS 2022](https://arxiv.org/abs/2207.08815) | Compares models on 45 tabular datasets; tree models perform strongly on medium-sized samples. | Start with small boosted trees, regularization, and a simple volatility baseline. |
| [Guo et al., ICML 2017](https://arxiv.org/abs/1706.04599) | Studies the gap between confidence estimates and observed accuracy, and evaluates calibration methods. | Treat probability calibration as a separate gate from ranking quality. |

These papers motivate the experiment. The improvement reported below comes from
our saved market data. The feature recipe and 4% attention target are our design
choices. An ML model's internal attention weights and the product's attention
score answer different questions.

## The candidate

The model receives 20 values available at the scan time:

- Absolute price changes over the session, five minutes, fifteen minutes, and the
  previous five minutes; recent volatility.
- Logged relative volume, recent relative volume, dollar volume, recent dollar
  volume, and average dollar volume.
- Quote age and time within the trading session.
- Movement relative to recent volatility, a change in relative volume, and
  whether the latest two short moves share a direction.
- Median movement, volatility, and relative volume across the scan, plus each
  stock's movement and volatility relative to those medians.

All price features preserve the same value when the directions are reversed.
Missing values remain missing. Peer features require a shared decision time.
The volatility field uses the scanner's last six returns; it is a recent measure
that can include the current move. A longer history of volatility remains a
separate future experiment.

Training uses LightGBM: 120 rounds, seven leaves, depth three, learning rate
0.05, minimum leaf size 100, L2 penalty 5, one thread, and a fixed seed. Each
training date has equal total weight. Two trained variants were specified:
market inputs alone, and market inputs plus context. Validation selected the
context variant. The settings stayed fixed through the test.

For a future rollout, the proposed market contribution is:

```
market points = 80 * calibrated chance of a material move in the next hour
```

The calibration set, eligible input ages, fallback policy, and complete final
score need prospective validation. Filing/news/social/cluster contributions and
urgent ordering must be evaluated together with this new market component.

## Data and target

The read-only export contains 81,969 saved snapshots from August 25 through
September 23. The study includes 26,908 rows: regular-session decisions between
09:30 and 15:00 New York time, with quote ages from zero through 45 minutes.
That age limit defines the study population; the existing action policy has its
own stricter limit. The market score uses the quote's actual age at the decision.

Outcome collection was incomplete on many recent snapshots. The study therefore
reconstructs a separate symmetric outcome from archived Yahoo five-minute bars:

1. Start at the first full bar opening at or after the decision.
2. Use that bar's opening price as the reference.
3. Require twelve consecutive bars whose last collection time is after completion.
4. Mark a hit when any high reaches +4% or any low reaches -4% within those bars.
5. Keep a missing or partial window as unknown, including a window with an early hit.

This gives 22,294 resolved windows, 4,544 with missing bars, and 70 with a partial
bar. The reference price comes from the future window's first open, so a delayed
snapshot price cannot create an apparent forward move. Both directions use the
same threshold and coverage rule. The established +8%/-4% forecast contract
continues to belong to the separate directional forecast.

Whole dates determine the 60/20/20 split before outcomes are inspected. The
program also checks label-window overlap at the boundaries.

| Partition | Date range | Decision dates | Rows | Resolved windows |
| --- | --- | ---: | ---: | ---: |
| Train | August 25–September 9 | 10 | 15,005 | 12,384 |
| Validation | September 10–17 | 4 | 3,898 | 3,379 |
| Test | September 18–23 | 4 | 8,005 | 6,531 |

Dates with qualifying saved decisions form these ranges. Every score ranks the
same rows, including unknown outcomes. Each scan gets ten review slots. Repeated
scans can select the same stock, so selections are the measurement unit.

## Held-out results

There are 34 test scans and 340 review slots.

| Market ranking | Observed hit selections | Hits / all slots | Resolved selected windows |
| --- | ---: | ---: | ---: |
| Current formula | 143 | 42.1% | 87.1% |
| Recent volatility alone | 140 | 41.2% | 69.4% |
| Selected context trees | **172** | **50.6%** | 77.4% |

With each date weighted equally, the observed hit rate rises from 41.98% to
50.38%: **+8.40 percentage points**. The candidate improves this measure on each
test date. A paired bootstrap that resamples whole dates gives a 95% interval
of +6.25 to +12.40 points. Four dates give a coarse uncertainty estimate: an exact
two-sided sign test for four positive dates has p=0.125. A longer prospective
trial is required for a strong statistical claim across market conditions.

The missing-window check compares the changed selections and cancels shared
selections. Across these exact dates, the possible mean gain is **+0.90 to +25.66
points**, allowing each unknown outcome to be either a hit or a miss. This is a
bound on this sample, separate from uncertainty about later dates. Some single
dates have bounds that cross zero.

On resolved test rows, the model's Brier score is 0.04695, compared with 0.06917
for a constant prediction from the training event rate. Lower is better. Ranking
and this error measure support further work; a dedicated calibration study is
still needed before probability percentages reach the product.

Training split gains are largest for recent volatility and volatility relative
to peers. The validation comparison also favors context: 56.08% observed hits
per slot with equal date weight, versus 53.81% for the market-only trees. Split
gain describes model usage; it gives limited evidence about any feature's
independent contribution.

## Evidence and replay

- [Machine-readable report](attention-study-2026-09-24/report.json): date membership,
  all validation scores, selected test scores, bounds, source hashes, and runtime.
- [Frozen candidate](attention-study-2026-09-24/attention-candidate.txt): the model
  selected on validation, with its SHA-256 in the report.
- `runner_web.attention_study`: target reconstruction, features, training,
  selection, evaluation, and per-row receipts.
- `scripts/export-attention-study.py`: bounded market-only PostgreSQL exports
  using a read-only transaction.

The original input files and per-row receipts are retained locally under
`data/attention-study-2026-09-24/`. The report hashes identify the exact files.
The first snapshot export includes older outcome fields used for the data audit;
the model and target use the explicit fields in the study code. The reusable
exporter emits only the market columns needed by the study.

```sh
PYTHONPATH=src python -m runner_web.attention_study \
  --snapshots data/attention-study-2026-09-24/attention-market-snapshots.jsonl \
  --bars data/attention-study-2026-09-24/attention-market-bars.jsonl.gz \
  --output data/attention-study-2026-09-24/replay
```

Fresh exports create a new dataset because saved bars can be revised. Exact
replay uses the retained files. The archive supplies its latest bar revision;
the study is a retrospective reconstruction. Source revisions, complete-window
selection, the scanner's saved universe, and four test dates limit generalization.
Returns after costs and the complete public board are separate evaluation targets.

## Next decision

Freeze this candidate for a shadow trial over at least 20 new trading sessions.
Record both rankings, the full score contributions, frozen feature vectors,
quote ages, collection times, and every outcome gap. Measure the same top-ten
review budget with date-level uncertainty. Check early/late sessions and
volatility groups separately. Use fresh data for calibration, then reserve later
dates for the promotion decision. The [live trial](attention-shadow-release.md)
now supplies that recording path. Its first ten complete sessions are reserved
for calibration. A later calibrated version needs at least twenty fresh sessions
before the public release decision.
