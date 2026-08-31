# SEC Qwen training

Runner Watch can backfill and export its archived SEC evidence as a frozen chat corpus, then train
the FERAL-7B Qwen LoRA adapter in a digest-pinned container. ilXyr remains the control plane for
corpus identity, cloud job state, budgets, and signed evidence.

## What the model learns

Corpus v2 creates several deterministic tasks from each accession:

- structured filing analysis and filing classification;
- semantic document chunks with exact source hashes and normalized character spans;
- point-in-time XBRL fact extraction and period comparison; and
- explicit insufficient-evidence answers.

Every answer is derived from archived SEC bytes, stored filing metadata, or deterministic math. No
teacher model writes labels. Facts filed after the accession are excluded, as are later prices and
returns. The adapter is an SEC reader, not a replacement for deterministic rug gates or the Rust
price ranker.

## Historical backfill

Set an SEC user agent that names the application and gives a contact URL or email. Backfill the
current Runner universe after applying database migrations:

```bash
export SEC_USER_AGENT='RunnerWatch SEC research contact@example.com'
stonks-migrate
stonks-sec-backfill \
  --database-path data/runner-watch.db \
  --years 3 \
  --max-filings-per-issuer 40 \
  --requests-per-second 2
```

The default universe is the intersection of Runner scan snapshots and the SEC company map. On the
current database this is 314 CIKs. The command reads each issuer's submissions JSON and only the
historical submission shards that overlap the requested date range. It downloads up to 40 recent
filings per issuer, balanced across form families, plus Company Facts. Use repeated `--cik` values
for a smaller named set.

Every response goes through the existing immutable source archive. A content hash, source URL,
collection run, and per-item state are stored. A completed issuer/date/form range is skipped on the
next run. Failed or deliberately bounded work stays pending and is safe to resume. A resume reuses
verified submissions, historical shards, and filing documents from the archive before making a
network request. The real client accepts SEC HTTPS hosts only, retries temporary errors, respects
`Retry-After`, and defaults to two requests per second. `--max-documents 1 --issuer-limit 1
--skip-company-facts` is a safe smoke run.

The SEC currently publishes a ten-request-per-second fair-access ceiling. Keep the default lower
rate, run one backfill process at a time, and do not bypass an SEC block. Filing exhibits can have
rights that differ from US government material; review rights before redistributing archived bytes.

## Export

Export the deterministic v2 corpus from SQLite or PostgreSQL:

```bash
stonks-sec-export-v2 exports/feral-7b-sec-v2 \
  --repository https://github.com/atimics/runner-watch \
  --revision FULL_40_CHARACTER_GIT_COMMIT \
  --source-path exports/feral-7b-sec-v2 \
  --dataset-id dataset://stonks/feral-7b-sec/v2 \
  --database-path data/runner-watch.db
```

The exporter refuses to overwrite a non-empty output directory. Its default caps are 32 filings per
issuer, eight semantic chunks and 20 total examples per accession, 240,000 source characters per
document, and four archived documents. Filings are balanced across form families before the issuer
cap is applied. Adjust caps in a new frozen experiment instead of silently changing an existing
corpus.

V2 avoids repeating evidence that does not teach a task. Classification uses filing identity,
structured analysis gets one representative chunk, comparisons get only the two facts being
compared, and insufficient-evidence examples get a compact fact inventory. Evidence-navigation
examples still contain the exact semantic chunk. This keeps citation behavior while reducing paid
token passes.

The split policy first holds out whole issuers by a stable CIK hash. It then splits the remaining
accessions by filing-time groups into train, validation, and future test data. All chunks and tasks
from one accession are locked to one split. The output includes four JSONL files, a summary with
task and estimated token counts, and an `ilxyr.corpus_release.v1` manifest with every SHA-256 and
byte size.

Filing exhibits can carry rights that are different from US government material. The generated
manifest uses `NOASSERTION`; review source rights and SEC access terms before redistributing a
corpus.

## Freeze and materialize in ilXyr

Publish the frozen release through the authenticated ilXyr corpus service. The command verifies
every local file against `corpus-release.json` before it sends anything. The token is read only
from the environment. The receipt records the artifact ref returned by ilXyr and explicitly keeps
training unauthorized:

```bash
export ILXYR_CORPUS_TOKEN='replace-with-a-random-secret-of-at-least-32-bytes'
stonks-sec-publish-ilxyr exports/feral-7b-sec-v2 \
  --service-url http://127.0.0.1:8787 \
  --receipt artifacts/feral-7b-sec-v2/ilxyr-publication.json
```

Copy the listed files to versioned S3 or Azure storage, read them back, verify every hash, and build
an `ilxyr.corpus_materialization.v1` receipt with the registered corpus ref. A second idempotent
publication records it:

```bash
stonks-sec-publish-ilxyr exports/feral-7b-sec-v2 \
  --service-url http://127.0.0.1:8787 \
  --materialization artifacts/feral-7b-sec-v2/s3-materialization.json \
  --receipt artifacts/feral-7b-sec-v2/ilxyr-publication.json
```

To create an updated read-only registry projection at the same time, pass the current ilXyr
registry as `--registry-template` and a separate `--registry-output`. Only the corpus, its two
lifecycle stages, their resolved missing requirements, and the matching source head change. The
project stays blocked; the command does not add a dispatch, budget, baseline, adapter, or training
claim.

In the experiment, put the dataset handle in `datasets` and its exact corpus artifact ref in
`dataset_bindings`.

`feral-7b-ilxyr-experiment.example.json` is the matching v2 experiment card. Replace its corpus and
OCI-image placeholder digests only after those artifacts have been frozen, then submit the four
referenced lineage records before compiling the experiment.

## Train and evaluate

The package in `ml/sec-qwen` has an exact dependency lock and requires a pinned base image. Copy
`config.example.toml`, keep the model's full 40-character Hugging Face revision, and validate before
starting an expensive job:

```bash
cd ml/sec-qwen
uv sync --frozen
sec-qwen validate config.toml
sec-qwen profile config.toml \
  --tokens-per-gpu-hour MEASURED_CALIBRATION_THROUGHPUT \
  --gpu-hour-price CURRENT_PROVIDER_PRICE \
  --output ../../../artifacts/feral-7b-sec-v2/profile.json
sec-qwen train config.toml
sec-qwen evaluate config.toml \
  --adapter ../../../artifacts/feral-7b-sec-v2/adapter \
  --split test-future.jsonl \
  --predictions ../../../artifacts/feral-7b-sec-v2/test-future.predictions.jsonl
```

For the genesis SEC baseline, run the same `evaluate` command without `--adapter`. This keeps the
base and candidate prompts, batching, generation settings, and exact-field scorer identical.

Freeze the public FinQA test set from an exact upstream revision, then build deterministic
citation-support cases from the corpus's sealed evaluation splits:

```bash
sec-qwen prepare-finqa /path/to/FinQA/dataset/test.json finqa.jsonl \
  --source-revision FULL_40_CHARACTER_FINQA_COMMIT
sec-qwen prepare-citation-support config.toml citation-support.jsonl
```

Run both base-model baselines without `--adapter`; add the candidate adapter later while keeping the
same dataset files and prediction IDs:

```bash
sec-qwen benchmark-evaluate config.toml \
  --dataset finqa.jsonl \
  --task finqa \
  --predictions baseline-finqa.jsonl
sec-qwen benchmark-evaluate config.toml \
  --dataset citation-support.jsonl \
  --task citation_support \
  --predictions baseline-citation-support.jsonl
```

Each prepared suite has a sidecar manifest with the exact source revision, file digest, size, and
example count. Citation-support treats malformed or missing confidence as fully confident and
therefore fails closed.

## Budget guardrail

Use `sec-qwen profile` to size the run. It uses the exact pinned tokenizer and chat template, reports
tokens by task and split, identifies truncation or examples with no trainable answer, calculates
optimizer steps, and can turn a measured throughput and current GPU price into a cost ceiling.
Benchmark 1% of the frozen train split on the exact image and GPU, then pass that measured
tokens-per-GPU-hour value to the profiler.

```bash
sec-qwen calibrate config.toml \
  --sample-fraction 0.01 \
  --output ../../../artifacts/feral-7b-sec-v2/calibration
```

The calibration uses a deterministic example-ID hash sample, runs the real LoRA path, saves no
adapter, and emits a receipt with `training_authorized` set to false.

```text
estimated GPU hours = full token passes / measured tokens per GPU hour
budget = estimated GPU hours * provider GPU-hour price * 1.25
```

The profiler's default extra 25% covers evaluation, checkpoint upload, and normal startup variance.
Record the generated profile, calculated limit, and provider quote in ilXyr before dispatch. Stop if
corpus hashes, image digest, token count, hardware, or provider price differ from the approved run.

The example config uses four deterministic data-loader workers and evaluates two prompts per batch.
Lower either value if the chosen executor has limited CPU or GPU memory. Both values are part of the
hashed training config, so a performance change cannot silently alter a replay.

Run evaluation separately on `test-future.jsonl` and `test-unseen-issuer.jsonl`. The command prints
exactly three metrics under a `metrics` object:

- `sec_json_valid_rate`
- `sec_field_exact_rate`
- `sec_example_exact_rate`

Preregister the primary threshold before training. Do not tune on either test split.

The checked-in example uses the image's Qwen2.5-7B-Instruct base at exact Hugging Face revision
`a09a35458c702b33eeacc393d103063234e8bc28`. The harness can also use another Qwen causal-LM
checkpoint, but that is a different frozen experiment.

Pass both the model ID and that revision while building the OCI image. The build caches the exact
snapshot and the running image forces Hugging Face and Transformers offline. This makes denied
runtime network access compatible with loading the base model.

FERAL-7B's release scorer also compares candidate and base-model outputs on the same sealed FinQA
IDs and the same deterministic citation-support cases. The default gate requires an 8 percentage
point FinQA improvement, no increase in confident unsupported answers, and at least 70% exact SEC
fields. It emits `feral_release_gate` as 1 only when all three conditions pass.

Training writes a deterministic uncompressed `adapter.tar`, its digest and size, the model and
corpus refs, the config digest, seed, and installed dependency versions. Upload the tar to versioned
object storage. The cloud adapter then records its URI, digest, size, media type, and provider
version in the ilXyr OCI completion. `sec-qwen completion` builds that schema-checked handoff from
the release metrics, uploaded adapter identity, and ilXyr dispatch ref.

## Cloud boundary

The same image can run in SageMaker, Azure ML, or a Kubernetes Job. The provider adapter must use:

- the OCI image digest frozen by the experiment;
- the exact ilXyr corpus materialization;
- network denial during the job;
- a versioned output location; and
- a trusted signing key for the completed ilXyr run digest.

The harness does not contain cloud credentials. Completion is resumable, but ilXyr will not promote
the result until the dispatched executor signs it.
