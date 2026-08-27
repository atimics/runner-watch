# Flash forecast evaluation design

Status: implementation baseline complete; public claims still follow the rollout gates
Date: 2026-08-27

## Decision summary

Evaluate one small, frozen market forecast inside each Flash report. Do not call the result the
accuracy of the whole report. A price result cannot prove that the report's facts or explanation
were correct.

The first public contract should be:

- Flash chooses `up`, `down`, or `no_call`.
- Flash gives an explicit probability that the price will be up.
- The app freezes the price and time that Flash saw.
- The result is measured at the regular-market close of the next trading session.
- An `up` or `down` forecast is a hit only when the move in the forecast direction is greater than
  0.5%. All other valid directional results are misses.
- `no_call`, pending results, and invalid market data do not enter the hit-rate denominator. Their
  counts stay visible.
- Every material model, prompt, context, schema, or risk-policy change creates a new public Flash
  version. Old and new results are never mixed.
- The public page shows counts before it promotes a percentage. Show a headline hit rate after 20
  settled directional forecasts. Call a version ready for comparison only after at least 50
  settled forecasts, 10 tickers, and 10 trading days.

The UI should call this the **Flash forecast record**, not "AI accuracy."

## Why the current record is not ready for users

The project already has useful parts, but they measure different things.

1. The hit rate on Pulse comes from old `kol_calls`. Those calls were made by a fixed ranker signal
   policy. The current Flash language model did not author them.
2. Pulse places Flash's current model name beside that old signal-policy hit rate. This can make a
   user think the current model earned the result.
3. Daily Flash reports use `one_shot_system_context`. Their output does not require a direction,
   probability, or fixed horizon, so most reports are not falsifiable forecasts.
4. `research_policy_scorecards()` only scores `verified_agent_pipeline` reports that are linked to
   completed user cases. It does not score normal Daily Flash reports.
5. Current Yahoo bars are stored unadjusted and corporate actions are not fully connected. A reverse
   split can create a false hit or miss.

Do not join these records or backfill a direction from old report prose. Historical reports should
say: **Not scored — this report predates the forecast contract.**

## What is being judged

Each new report contains two separate products:

| Part | What it means | How it is checked |
| --- | --- | --- |
| Evidence report | Facts, sources, risks, and unknowns | Citation rules, source checks, review, and corrections |
| Market forecast | A frozen price direction and probability | Later market prices under one fixed contract |

A market hit does not clear an unsupported factual claim. A market miss does not prove every fact in
the report was wrong. The report page should keep these ideas separate.

## Forecast contract v1

Name the first contract `flash-next-session-v1`.

### Forecast fields

The model must return this block. The app must not infer it from the thesis.

```json
{
  "forecast": {
    "direction": "up",
    "probability_up": 0.68,
    "reason": "The filing and volume support the move, but dilution risk remains."
  }
}
```

Rules:

- `direction` is `up`, `down`, or `no_call`.
- `probability_up` is a number from 0 to 1. It means one thing only: the chance that the valid end
  price is above the frozen start price.
- `up` requires `probability_up >= 0.55`, `down` requires `probability_up <= 0.45`, and `no_call`
  uses the uncertain range between them. A different band requires a new output schema version.
- `reason` is short and must agree with the report.
- The server owns the horizon and scoring rule. The model cannot choose them.
- `mixed`, `watch`, `avoid`, and `exit` are report or risk states. They are not hidden aliases for a
  direction. If Flash does not have a directional view, it must use `no_call`.
- A deterministic risk veto remains separate from the forecast. `AVOID` must not be silently changed
  into `down`.
- For a new scored version, missing or malformed forecast fields fail generation and refund the
  Flash charge. Normalization may not invent a forecast.

### Frozen start

Freeze these values before the model request:

- ticker and exchange;
- evidence timestamp and evidence fingerprint;
- price, price timestamp, source, and market session;
- requested model and full Flash version;
- the evaluation contract.

The price must be the same price included in the model evidence. This prevents look-ahead and avoids
starting the score after the model has answered.

Eligibility rules:

- During an open session, the price must pass a fixed freshness rule. Start with 10 minutes.
- Outside an open session, use the last regular-market close and label it clearly.
- Missing, non-positive, or clearly broken prices make the forecast unscored.
- Do not quietly replace a bad start price after seeing the result.

### End price

Use the last valid regular-hours bar from the first trading session after the forecast's US Eastern
calendar date. This matches the current meaning of the app's `1d` outcome better than the old
60-minute ranker contract.

Examples:

- A report made Tuesday at 11:00 ET is judged at Wednesday's regular close.
- A report made Friday after hours is judged at Monday's regular close, unless Monday is a holiday.
- An early close uses that session's real last regular-hours bar.

Do not calculate session dates from weekdays alone. Use an exchange calendar or verified observed
bars so holidays and early closes are handled correctly.

### Hit and miss

For a valid directional forecast:

```text
return_pct = (end_price / start_price - 1) * 100
signed_move = return_pct for up, or -return_pct for down
hit = signed_move > 0.5
miss = signed_move <= 0.5
```

The 0.5% floor matches the conservative 50 basis point round-trip cost already used by the old paper
Call record. It stops a tiny move from looking like a useful hit.

Keep a miss reason for the detail view:

- `wrong_way`: the price moved more than 0.5% in the other direction;
- `no_meaningful_move`: the result did not clear the 0.5% floor.

A conflict between direction and probability is a generation validation error. It should never
become a settled result.

The floor is part of the contract. Changing it creates a new contract, and rates from the two
contracts must not be compared as if they were the same measure.

### Other result states

| State | Enters hit rate? | User copy |
| --- | --- | --- |
| `pending` | No | Judged after the next trading session |
| `hit` | Yes | Hit · +x.x% in the forecast direction |
| `miss` | Yes | Miss · wrong way or did not clear 0.5% |
| `no_call` | No | Flash made no directional call |
| `void_split` | No | Not scored · corporate action changed the price basis |
| `void_no_data` | No | Not scored · no reliable closing price |
| `under_review` | No | Result under data review |
| `corrected` | Depends on corrected result | Corrected result with a visible reason and date |

Halt, delisting, symbol change, and corporate-action rules must be written before launch. Voided
results remain visible so exclusions cannot hide bad calls.

## Public metrics

### Main numbers

For each Flash version, show:

- hits;
- misses;
- pending;
- no-call reports;
- void or reviewed results;
- settled directional forecasts;
- distinct tickers;
- distinct trading days;
- forecast coverage: directional forecasts divided by eligible completed reports;
- hit rate: hits divided by settled directional forecasts;
- 95% Wilson interval for the hit rate;
- median signed move after the 0.5% floor;
- report completion rate.

The hit rate is easy to read, but it is not enough by itself. Coverage stops a version from looking
great by making almost no calls. The sample and uncertainty stop a short lucky streak from looking
settled.

### Probability quality

Keep probability quality beside, but quieter than, the hit record:

- Brier score for `probability_up` on results with a clear up or down move;
- a simple calibration view after enough samples;
- the number of forecasts in each probability group.

Brier score is a proper scoring rule. It rewards honest probability estimates instead of rewarding
only the side of 50% on which the forecast landed.

Compare Brier score with both a fixed 50% forecast and the observed base rate for the same frozen
cohort. A score below 0.25 alone does not prove that one version is better in every market cohort.

Do not publish a ten-bin calibration chart with two items per bin. Start with broad probability
groups and only when each group has a useful sample.

### Display thresholds

Use states that a normal user can understand:

| State | Rule | Display |
| --- | --- | --- |
| Building record | Fewer than 20 settled directional forecasts | Counts only; no headline percentage |
| Early record | 20 to 49 settled forecasts | Hit rate, sample, and wide uncertainty label |
| Comparable | At least 50 settled, 10 tickers, and 10 trading days | Version comparison is allowed |
| Retired | No longer generating new reports | Permanent frozen scorecard |

The current product promotion checks of 50 outcomes, 10 tickers, a Wilson lower bound above 50%,
and Brier below 0.25 are a good base. Add trading-day diversity and forecast coverage. Do not promote
from hit rate alone.

## Version rules

Flash is the lasting public character. A Flash version is the exact system that produced a forecast.

A material change creates a new version when it changes any of these:

- requested or resolved model;
- prompt instructions;
- output schema;
- context selection or evidence schema;
- deterministic risk policy;
- report pipeline;
- forecast contract.

CSS, spelling, and other presentation-only changes do not create a new version.

Use an explicit release row such as:

```text
public label: Flash 2026.09
version id: flash-2026-09-a
model label: GLM 5.3
requested model id: z-ai/glm-5.3
allowed resolved model id: z-ai/glm-5.3
prompt version: daily-flash-v2
context version: identity-thesis-v1
risk policy: market-risk-v3
output schema: flash-report-v2
forecast contract: flash-next-session-v1
```

Do not build the public version only from the model name. A prompt or risk-policy change can change
behavior without changing the model. If a provider model alias can change weights without changing
its ID, say "provider model ID" in the detail view and use pinned model IDs when available.

Store the model ID returned by the provider on every forecast. If it does not match the active
version's allowed model, stop it from entering that version's totals and raise a configuration-drift
alert. Do not silently accept the mismatch.

The current version page must not rewrite retired versions. A report always keeps the version that
made it.

## Fair version comparisons

Live reports are chosen by users. One version may receive biotech runners while another receives a
different mix of tickers and market conditions. Their raw hit rates are real live records, but they
are not automatically a fair model battle.

Keep two tracks:

1. **Live record:** every eligible user-generated Daily Flash forecast. Show this to users.
2. **Matched evaluation:** champion and candidate versions run on the same frozen evidence and the
   same timestamp. Use this for promotion decisions.

For a candidate rollout:

- decide the sample size and contract before starting;
- assign each frozen case to both versions in shadow, or use a stable random split if cost prevents
  paired runs;
- do not change the finish line after seeing results;
- compare the same tickers, sessions, and outcome rules;
- keep candidate output private until the release decision if public shadow calls would confuse the
  product;
- publish the test method and final sample when claiming that a version improved.

Wilson intervals are useful for describing one live record. They do not make repeated tickers and
same-day market moves independent. For a promotion decision, compare paired outcomes and group the
uncertainty check by trading day so one broad market move does not look like many separate wins.

The user page may list live records by version. It should only use words such as "better" or
"improved" after a matched evaluation supports that claim.

## Data model

Add three small tables. Keep the forecast separate from the later result.

### `flash_versions`

One immutable row per public version:

- `id`, `public_label`, `actor_id`, `status`;
- `provider`, `requested_model`, `allowed_resolved_model`;
- `prompt_version`, `context_version`, `risk_policy_version`;
- `output_schema_version`, `pipeline_version`, `forecast_contract_version`;
- `configuration_fingerprint`;
- `launched_at`, `retired_at`, `created_at`.

### `flash_forecasts`

One immutable row per scored or explicitly unscored report:

- `id`, `report_id` unique, `version_id`, `actor_snapshot_json`;
- `provider`, `requested_model`, `resolved_model`, `provider_request_id`;
- `ticker`, `exchange`, `evidence_key`, `evidence_as_of`;
- `direction`, `probability_up`, `reason`;
- `start_price`, `start_at`, `price_source`, `market_session`;
- `contract_version`, `target_session_date`;
- `eligibility`, `ineligibility_reason`;
- `created_at`.

Use database checks for direction, probability, price, and state. Store normal metric fields in
columns, not only inside JSON.

### `flash_forecast_outcomes`

One current result per forecast:

- `forecast_id` primary key;
- `status`, `classification`, `miss_reason`;
- `end_price`, `observed_at`, `return_pct`, `signed_move_pct`;
- `max_favorable_pct`, `max_adverse_pct`;
- `bar_source`, `bar_fingerprint`;
- `corporate_action_state`, `void_reason`;
- `first_checked_at`, `resolved_at`, `updated_at`.

Add an append-only `flash_evaluation_events` table for `created`, `resolved`, `voided`, and
`corrected` events. A correction must keep the old values in the event payload, name the reason, and
show a correction note on the report.

Do not copy the user ID into the public evaluation tables. The report link is enough and avoids an
extra privacy join.

## Write and evaluation flow

1. The server freezes evidence, a usable price, and the active Flash version.
2. The model receives the fixed forecast contract with the evidence.
3. The server validates narrative, citations, direction, and probability.
4. The report and forecast are committed in one database transaction before the report can be read
   or published.
5. Publication timing does not change the forecast start. Early publishing cannot improve or delay
   the score.
6. The outcome worker checks due forecasts, fetches and archives the needed bars, applies corporate
   action rules, and writes one final result plus an event.
7. The record cache is cleared and all pages read the stored receipt. A page request never calculates
   a new result from a live quote.

Failures are separate from forecast misses. A provider error affects completion rate and refunds the
user, but it is not a market miss.

## User experience

### Report page

Place a compact receipt under the Flash author row.

Pending example:

```text
FORECAST · FLASH 2026.09
Up · 68% chance up
From $1.23 at 11:05 ET · judged after next session close
Pending
```

Settled example:

```text
FORECAST RESULT
HIT · +6.2%
$1.23 → $1.31 · next session close
Part of Flash 2026.09: 13 hits / 7 misses
```

Add a short note: **This result judges the price forecast, not every fact in the report.**

### Flash record page

Add `/flash/record` with four parts:

1. Current version, status, and exact contract in plain English.
2. Counts and hit rate, with sample and uncertainty beside the percentage.
3. Version history. Each row shows dates, model, hits, misses, no calls, voids, and sample.
4. Permanent result ledger. Each row links to the original report and shows ticker, forecast,
   probability, start, end, move, and result.

Suggested current-version copy:

```text
Flash 2026.09 is building its live record.
13 hits · 7 misses · 4 no calls · 3 pending
65% hit rate across 20 settled forecasts
10 tickers · 8 trading days
```

The exact contract should be one tap away, not hidden in terms text.

### Entry points

- Link the Flash author row on every report to the record.
- Add **Flash record** to the profile sheet.
- Make the Pulse Flash strip link to the record.
- Replace or clearly relabel the existing Pulse paper-call strip. If it remains, call it **Legacy
  signal policy** and remove the current language-model label.
- Keep old paper Calls on their own page or section. Never add them to report-version totals.

### Words to avoid

Avoid claims such as "AI accuracy," "best AI," "proven winner," or "beats the market." The honest
labels are "forecast record," "live user-selected reports," "hit rate under this contract," and
"early sample."

## API shape

Add a focused read endpoint:

`GET /api/flash/record`

```json
{
  "contract": {
    "id": "flash-next-session-v1",
    "horizon_label": "next regular session close",
    "minimum_move_pct": 0.5
  },
  "current_version": {
    "id": "flash-2026-09-a",
    "label": "Flash 2026.09",
    "model_label": "GLM 5.3",
    "state": "early",
    "hits": 13,
    "misses": 7,
    "pending": 3,
    "no_calls": 4,
    "voids": 0,
    "settled": 20,
    "hit_rate": 0.65,
    "hit_rate_interval_95": [0.433, 0.819],
    "distinct_tickers": 10,
    "distinct_trading_days": 8,
    "forecast_coverage": 0.833
  },
  "versions": [],
  "recent_results": [],
  "next_cursor": null
}
```

Use a cursor for the full ledger. Keep `/api/kols` backward compatible while old clients use it, but
do not make that mixed endpoint the new public contract.

## Corporate actions and bad data

This is a launch blocker, not polish. Penny stocks reverse split often, and current stored Yahoo
bars are unadjusted.

Before a public hit rate:

- connect a reviewed corporate-action source or a price source with clear adjustment rules;
- store split factor, effective time, old symbol, new symbol, and source;
- test reverse splits, symbol changes, mergers, cash-outs, halts, and delistings;
- show every void and correction;
- alert when the void rate is high or differs by version;
- keep the exact start and end price receipts permanently, even after raw bar retention expires.

Shadow evaluation may start sooner, but its score should stay internal until these rules pass a
manual sample review.

## Safety and product review

A visible success rate beside AI opinions can make the product feel more like a recommendation
service even when the interface does not execute trades. Canadian guidance says securities rules
apply regardless of whether the speaker is a person, avatar, or AI, and that opinions on the merits
of investing can be advice.

Before the public release, get a focused Canadian securities review of:

- the forecast wording and probability display;
- the Flash wallet and publish reward beside performance claims;
- whether the record page changes how a normal user reads the reports;
- correction, conflict, issuer relationship, and sponsored-content rules;
- record retention and the meaning of the disclaimer.

This design is a product and measurement proposal, not a legal opinion.

## Rollout plan

### Phase 0 — fix attribution now

- Stop placing the current Flash model name beside old ranker-policy results.
- Keep the legacy endpoint and receipts, but label them as a separate signal-policy record.
- Add explicit public version rows.

### Phase 1 — shadow receipts

- Add the required forecast block, tables, outcome worker, and internal scorecard.
- Do not backfill old prose.
- Run at least 20 outcomes and manually replay every receipt.
- Check holidays, stale quotes, after-hours starts, missing bars, halts, and reverse splits.

### Phase 2 — individual public results

- Show pending and settled receipts on new report pages.
- Keep the aggregate percentage hidden until the sample gate passes.
- Ship corrections and void visibility at the same time.

### Phase 3 — public version rate

- Keep `/flash/record` in building-record mode until 20 settled directional forecasts and data
  review. Show counts, but do not promote a percentage before then.
- Show sample, coverage, uncertainty, and contract beside the rate.
- Monitor void rate, missing-price rate, generation completion, and outcome delay.

### Phase 4 — version promotion

- Run candidate and champion on a matched frozen cohort.
- Require the predeclared comparison sample and product-policy gates.
- Retire the old version without changing its reports or scorecard.
- Publish a short change note that says what changed and how the comparison was run.

## Test plan

At minimum, add tests for:

- required forecast fields and valid `no_call`;
- no inferred forecast from narrative text;
- exact version snapshot when the live assignment changes;
- atomic report and forecast creation;
- private-hour publishing not changing start time;
- next-session selection across weekends, holidays, and early closes;
- up hit, down hit, wrong-way miss, and no-meaningful-move miss;
- stale price and missing price;
- missing bars and delayed bars;
- halt, reverse split, and symbol change;
- one final result per forecast and safe worker retries;
- visible correction history;
- hit-rate denominator excluding pending, no-call, and void;
- Wilson interval and small-sample display state;
- version totals never mixing;
- legacy paper Calls never entering Flash report totals;
- API cursor stability and cache invalidation;
- mobile and desktop wording, keyboard focus, and screen-reader labels.

## Launch gates

Do not promote the aggregate hit rate as a product claim until all are true:

- forecast contract and version rules are frozen;
- every new public report stores a forecast or a clear unscored reason;
- at least 20 shadow results have been manually replayed;
- the corporate-action rule is working;
- missing and void results are visible;
- the old Pulse attribution is fixed;
- corrections are append-only and user-visible;
- the safety and Canadian product review is complete;
- monitoring alerts on delayed outcomes, version mixing, and abnormal void rate.

## External design basis

- [CSA and CIRO Staff Notice 31-369](https://www.ciro.ca/newsroom/publications/joint-canadian-securities-administrators-and-canadian-investment-regulatory-organization-staff)
- [NIST AI Risk Management Framework 1.0](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-ai-rmf-10)
- [Gneiting and Raftery, Strictly Proper Scoring Rules, Prediction, and Estimation](https://doi.org/10.1198/016214506000001437)
