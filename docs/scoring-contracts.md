# Stock scoring contracts and training integrity

Status: implemented foundation. This is an uncalibrated attention policy and
an evaluation-integrity change, **not evidence of improved trading returns**.
The current outcome models are retained. New GAM/forest training produces shadow
candidates, not automatic production replacements.

## One measurement per output

| Output | Contract |
| --- | --- |
| `score`, `attention_score`, `custom_score` | Aliases of `attention-activity-v3`, in **heuristic points**, not percent or probability. The same definition applies with and without an active model. |
| `forecast` | A validated three-way distribution for the recorded +8%/-4%/60-minute barrier contract. Probabilities are never modified by engagement, news boosts, or policy deductions. |
| `forecast.assumed_barrier_payoff_pct` | The model's assumed barrier-exit payoff before costs. It is neither executable profit nor a forecast of the terminal 60-minute return. |
| `rug_score`, `rug_level`, `hard_veto` | Existing heuristic structural-risk assessment and independent veto. These are **not** calibrated probabilities of fraud or loss. |
| `eligibility` | `eligible`, `blocked`, or `unknown`, with machine-readable reasons and separate booleans. It describes the scoring policy, not an order-execution authorization API. |

The lower-first probability is **not** the probability of ever losing 4% over the
horizon: a path can hit the upper barrier and then collapse. No total-drawdown or
expected-shortfall probability is fabricated in this release. A directional
abstention is called "No directional call", not "No edge"; a timeout does not
prove the absence of a useful return.

Malformed/non-finite probabilities, invalid sums and incompatible recorded label
contracts are not exposed as forecasts. Legacy predictions without a contract
are explicitly identified as `legacy_assumed_v1` rather than represented as
having a recorded contract.

## Direction-neutral attention

The market component uses saved raw activity, never the bullish setup score or
`100 * P(up)`:

```
volume   = min(35, 8 * log2(max(1, relative_volume, recent_relative_volume)))
momentum = min(30, max(6 * abs(momentum_5m_pct), 2 * abs(momentum_15m_pct)))
move     = min(15, abs(change_pct))
freshness = exp(-max(0, quote_age_minutes - 5) / 15)
market   = (volume + momentum + move) * freshness
cluster_scale = min(8, 2 * log10(1 + tracked_cluster_value / 10000))
cluster = cluster_scale * sqrt(min(1, stock_holding_value / tracked_cluster_value))
attention = clip(market + filing + news + external_social + cluster, 0, 100)
```

Missing numeric inputs contribute no points and are reported in
`activity_inputs.missing`. An unknown or future quote clock gives zero market
contribution and cannot be presented as fresh evidence. The returned availability
state distinguishes missing, partial and available activity.

Filing contribution is at most 12 points regardless of direction; news at most 6
and external social activity at most 8. The cluster contribution is at most 8
points. It uses saved prices for all tracked stocks held by entities directly
linked to the stock. The stock's share of that cluster value reduces the boost
for small positions. Missing prices or a zero stock holding give zero points.
The score does not claim that these holdings predict a return. Identical news
URLs are deduplicated.
These constants are declared heuristic policy, not learned parameters. News-event
clustering across syndication and independent-source validation remain future work.

Both internal Calls and comments contribute **zero** attention points. Their
counts remain visible but cannot reward the application's own exposure loop.
Existing evidence-family UI is not a statistical proof of independent evidence.

An observed active trading halt gets an explicit urgent flag. Ordering is:
urgent first, attention descending, saved baseline rank, ticker. Replay uses the
same ordering function. A halt may be first with an attention index of 10: urgency
is a disclosed policy override, not a fabricated prediction of 100% activity.
Other structural blocks do not automatically become urgent events.

## Eligibility and clocks

A known risk veto, AVOID/EXIT state, or HIGH/CRITICAL risk blocks. The numerical
fallback uses the existing HIGH boundary of 50, so an absent textual risk level
cannot evade the same policy. This aligns the score and existing public risk tag;
it does not claim that 50 points equals a 50% chance of loss.

The public scorer requires a finite positive price, a known quote age no greater
than 15 minutes, a recognized assessment state, and a finite risk index. Missing
checks return `unknown`; explicit blocks dominate unknowns. The evidence gate
cannot label a blocked or unknown assessment ready. The standalone eligibility
helper retains a permissive legacy default; the public integration always passes
`require_complete=True`.

The list and detail APIs preserve:

- `feature_as_of`: the underlying saved scanner feature snapshot;
- `quote_as_of`: the quote used by those features;
- `computed_at`: when the attention policy was evaluated.

A newer display quote does not refresh old feature vectors or clear an old risk
assessment. Detail for a ticker outside the latest universe still uses the same
attention policy on its older evidence; it does not fall back to a differently
meaningful score. Frozen research evidence keeps its original scanner score.

Legacy `policy_components` and deduction-shaped fields remain compatibility
receipts. They are not subtracted from attention and do not modify probabilities.

## Label collection and migration

A resolved barrier and an observed terminal return are separate facts. The outcome
worker now retries a missing 60-minute return even after the first-hit barrier is
known, and synchronizes the compact training row when that return arrives.
Unknown, invalid and non-finite returns remain SQL NULL; a measured zero remains 0.

Migration 82 repairs recoverable legacy errors without deleting observations:

1. Carries known same-bar ambiguity into source and compact resolution fields.
2. Clears zero-filled compact returns where the source terminal return is absent.
3. Marks historical-replay rows without resolution provenance `unverified_legacy`.

Historical replay writes explicit resolution from the labeler on regeneration.
The pessimistic `down` label on a both-barriers bar is retained for compatibility,
but ambiguous observations do not become resolved training facts.

The current integer training protocol needs both a resolved class and an observed
terminal return. Incomplete groups remain excluded instead of zero-filled. This
can reduce sample coverage and retains complete-case selection bias; it is not a
censoring model. Track coverage before comparing candidate performance. The
repair cannot recover zero-fills whose original observations were already pruned.

## Fixed-membership temporal splits

`purged_chronological_split` first fixes the original approximately 80/10/10
memberships using the Rust count convention. It then removes training intervals
that reach validation and validation intervals that reach test. Equality at a
boundary is overlap. Test membership is unchanged.

Every row supplies a real decision timestamp (`run_captured_at` or `captured_at`).
The conservative default label end is 60 minutes plus the existing 10-minute bar
tolerance, or an explicit `label_end_at`. Invalid/missing clocks fail rather than
silently disabling the purge. All row intervals in a group count.

The Python/Rust request supplies **actual post-purge train and validation counts**.
Rust must not re-split the concatenated retained groups by percentage. It rejects
empty or inconsistent explicit partitions. Legacy fractional Rust requests remain
accepted for compatibility but are labeled `legacy_fractional_split_unpurged`.
The production Python trainer always supplies the explicit counts, verifies the
returned split identifier and sizes (rejecting an old binary that ignores them),
and persists a split receipt. Deploy the Python and Rust changes together. Too little data or class support after purging cannot train.

GAM, forest and benchmark paths use the same partition helper. They reserve the
validation partition and report test results; these retrospective comparisons
are not equivalent to untouched prospective evaluation.

## Evaluation and promotion

The incumbent comparison reads Rust probabilities in **parts per million**, checks
batch identity and normalization, and invokes the command for the actual served
model kind. It does not silently fill missing float-named probability columns
with zeros.

Challenger reports include multiclass log loss, class-wise Brier error and binned
reliability gaps. Their base-rate comparator is fitted on training targets, not
the test prevalence. Direct descriptive metric calls without a training prior
are labeled `evaluation_descriptive_only`.

New candidate training records `candidate_improved` separately from `promoted`.
GAM and forest candidates remain shadow even if one retrospective comparison
improves. A prospective, cohort-aware gate and independent event support are
still required before enabling automatic promotion. The existing logistic
manual promotion policy is unchanged; this PR does not assert it now implements
block-bootstrap confidence intervals or multiple-testing correction.

Served-policy replay ranks the original candidate universe **before** examining
labels. Unresolved or ambiguous top-k items consume the review budget rather than
being replaced by conveniently labeled lower-ranked candidates. Reports show
coverage, observed precision, and missing-label lower/upper bounds. The current
material-activity proxy is hitting either asymmetric barrier, not every kind of
financially material news. Evaluate per scan, not by pooling incompatible universes.

## Bounded replay is not exact reconstruction

Scoring reads reject future predictions, filings collected/revised after the
cutoff, and events whose latest collected version is newer than the cutoff.
Future event timestamps are ignored even when calling the pure event helper.

Source approvals, model activation, company identity and community state still
lack a complete historical revision ledger. Later revisions are excluded rather
than reconstructed. `replay_status` and `replay_limitations` explicitly report
that limitation. Do not use this loader to claim exact historical production
performance; frozen feature vectors remain the training input.

## Rollout and remaining work

Review migration impact on complete-group counts before model experiments.
Expect changed public ordering and additional PAUSED/unknown states: this is an
intentional units/eligibility correction, not a model performance regression by
itself. Downstream attention thresholds need fresh validation under the named
policy. Baseline scanner features and probability storage units remain unchanged.

To revert application behavior, deploy the previous application version while
retaining the data-integrity repair. Do not turn ambiguous or missing observations
back into resolved zero returns just to recover the old sample count.

Still unimplemented: a calibrated material-event attention target; independent
maximum-adverse-excursion and loss-severity models; execution-aware utility;
point-in-time revision ledgers; exposure-aware learning; session-block confidence
intervals; prospective model promotion and regime monitoring. Those require
measured data, not new arbitrary probability weights.
