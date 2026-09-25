# Learned attention: live trial and release gates

This release starts the prospective trial. Each eligible scan saves the current
board beside the frozen research candidate. The public board uses
`attention-activity-v3`. Trial results belong to the calibration collection phase.

## Frozen contract

- Candidate: `attention-context-20260924-v1`, the context trees selected in the
  [market study](market-attention-study.md).
- Native model SHA-256:
  `a904b966dd915112379ab2f6b763ea55afc79216b1351100218386e446864965`.
- Packaged tree SHA-256:
  `ac6b8446b1ee99208ba4d8352c87d6e283d2db3537e0538d62903136c1e3a497`.
- Market contribution for this trial: `80 * raw model probability`. This is an
  uncalibrated trial value. Calibration creates a separate version for evaluation.
- Target: at least a 4% rise or fall from the next full five-minute bar's opening
  price over twelve full bars. Both outcomes require every bar in the window.
- Session: the XNYS exchange calendar, including holidays and early closes. The
  full outcome window must fit within regular trading hours.
- Features: the same twenty values used in the study. Quote age is measured at
  the saved evidence time. Eligible peers belong to that exact scan.
- Fallback: a quote age outside 0–45 minutes, an invalid feature clock, an invalid
  price, or a nonregular snapshot uses the existing market and board score.

## Durable records

Migration 85 adds three tables. They have their own keys so ordinary scan cleanup
can retain the trial evidence.

`attention_trial_runs` identifies the exact scan, model hash, policy, build,
contract, evidence time, committed prediction time, and future window. A scan can
be claimed once. Capture failures and interrupted captures remain visible.

`attention_trial_predictions` stores every board member, raw model inputs,
twenty feature values, probability, fallback reason, evidence contributions,
eligibility, urgent state, and four complete rankings: current and candidate
market scores, plus current and candidate full board scores. Filing, news,
social, and cluster boosts retain their caps. Urgent items stay first.

The predictions commit before the future window is chosen. The outcome starts
at the next full bar after that commit. This protects the comparison when a scan
or database write takes extra time. A crash between the two writes leaves a
`recording` receipt. Such a receipt requires an operator review and stays outside
the metrics.

`attention_trial_bars` saves completed Yahoo five-minute observations and later
revisions while a window is pending. Identical completed observations share a
hash. An unchanged bar seen after its close still gets its completed receipt.
Labels use the first completed observation of each bar. The outcome receipt
records all chosen hashes. Later revisions remain available for an audit.
An archive failure gets an ingestion error receipt. A separate database savepoint
preserves normal market collection through such a failure.

## Outcome worker and operations

`ATTENTION_SHADOW_ENABLED=1` enables scan capture, completed-bar recording, and
the `attention-shadow` worker. Fly sets this flag. Worker health includes that
task. To stop new trial work, deploy with the flag set to `0`; saved receipts stay
in the database.

The worker starts after 45 seconds and checks every two minutes. It follows up
to 30 tickers per pass, including stocks that have left the latest scan. It tries
existing receipts before requesting Yahoo data. Retries rotate through the queue
at five-minute intervals. At six hours after the outcome window closes, each
remaining gap becomes an `unknown` outcome with its reason and available hashes.
Unknown selections retain their review slots in the comparison.

Run inside the deployed worker environment:

```sh
python -m runner_web.attention_trial
```

The JSON report contains the latest saved time, run failures, outcome counts,
fallback reasons, complete session count, and separate full-board and market-only
metrics. Metrics use a single model hash and baseline policy. A session counts
as complete after its close plus the six-hour outcome allowance, once every row
has a final outcome. Date-weighted top-ten hit rates, coverage, a paired date
bootstrap, and bounds for missing outcomes are included.

After deployment, verify schema 85, the model hash, the worker heartbeat, and a
new trial receipt during the next eligible session. Inspect capture errors,
stuck `recording` rows, unresolved windows, database growth, and provider failures.
Compare quote-age and fallback counts before interpreting a ranking gain.

## Next release gates

1. Collect ten complete calibration sessions under this frozen contract. Review
   all failures and coverage by time of day, price, volume, and volatility.
2. Fit a simple calibration map using those sessions. Save its parameters, source
   hashes, cutoff, reliability results, and policy version in a new artifact.
   Freeze the map before collecting evaluation predictions. This is the next
   implementation milestone after live data arrives.

   Run `python -m runner_web.attention_calibration` against the trial database
   to inspect receipt readiness. Once ten complete sessions exist, pass
   `--out path/to/calibration.json` to save a candidate map. It fixes five raw
   probability intervals, applies one success and one failure of smoothing
   per populated interval, and pools adjacent intervals until mapped values
   are monotone. The artifact binds the model, baseline policy, contract, ten
   calibration dates, cutoff and digest of the selected run/prediction receipts.
   Unknown outcomes and fallback predictions remain counted; only resolved
   learned predictions fit the map. No map is emitted before ten complete
   sessions. This fitting command does not activate the map on the public
   board. Examine reliability by date and declared subgroups, then freeze a
   separate evaluation policy before applying the map to later sessions.
3. Collect at least twenty later sessions with the calibrated version. Evaluate
   the full board and its market contribution separately. Require at least 10%
   date-weighted top-ten observed-hit lift, a positive lower bound on the 95%
   date-level interval, at least 90% selected-window coverage for both rankings,
   and a positive paired lower bound after allowing for unknown outcomes.
4. Confirm calibration across the same groups. Set limits from the calibration
   phase before evaluation. Check fallback rate, latency, source outages, and
   full-board score changes. Keep a separate final release decision for the
   combined ranking, calibration, and operational evidence.
5. Introduce the learned order in a staged public rollout with an instant return
   to the current policy. Record the served policy on each request. Keep training
   changes behind the same future-data gates.

`promotion_ready` stays false during this calibration collection release. Its
reported numerical gates are descriptive evidence for the next milestone.

## Validation

The packaged Python trees are checked against LightGBM on finite, zero, and
missing features. The container build checks artifact loading and inference.
Tests cover full-board receipts, exact scan selection, fallback, urgent order,
commit timing, holiday and early-close windows, unchanged final observations,
bar revisions, missing windows, bounded retries, capture errors, idempotence,
schema upgrades, and retained unknown slots.

Reproduce the package from the saved native model:

```sh
PYTHONPATH=src python scripts/export-attention-model.py
```
