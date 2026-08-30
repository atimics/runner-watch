# SEC Qwen training

Runner Watch can export its archived SEC evidence as a frozen chat corpus and train the FERAL-7B
Qwen LoRA adapter in a digest-pinned container. ilXyr remains the control plane for corpus identity,
cloud job state, and signed evidence.

## What the model learns

Each example contains:

- one filing accession and its archived SEC text when available;
- issuer facts whose SEC filed time is no later than the filing;
- source URLs and hashes; and
- Runner Watch's existing structured parsing result as the assistant answer.

The answer is strict JSON for filing type, sentiment, score, named actor, ownership, and transaction
fields. It does not contain later prices or returns. The Qwen adapter is therefore an SEC reader and
structured extraction model. It does not replace the deterministic rug gates or the Rust price
ranker.

## Export

Apply database migrations, then export from SQLite or PostgreSQL:

```bash
stonks-migrate
stonks-sec-export exports/sec-qwen-v1 \
  --repository https://github.com/atimics/runner-watch \
  --revision FULL_40_CHARACTER_GIT_COMMIT \
  --source-path exports/sec-qwen-v1 \
  --dataset-id dataset://stonks/sec-filings-qwen/v1
```

For a local database, add `--database-path data/runner-watch.db`. The exporter refuses to overwrite
a non-empty output directory.

The split policy first holds out whole issuers by a stable CIK hash. It then splits the remaining
issuers by filing-time groups into train, validation, and future test data. One accession appears
once. Facts after the filing are excluded. The output includes four JSONL files, a summary, and an
`ilxyr.corpus_release.v1` manifest with every SHA-256 and byte size.

Filing exhibits can carry rights that are different from US government material. The generated
manifest uses `NOASSERTION`; review source rights and SEC access terms before redistributing a
corpus.

## Freeze and materialize in ilXyr

Register `corpus-release.json` with the ilXyr corpus service. Copy the listed files to versioned S3
or Azure storage, read them back, verify every hash, and record the materialization receipt. In the
experiment, put the dataset handle in `datasets` and its exact corpus artifact ref in
`dataset_bindings`.

`feral-7b-ilxyr-experiment.example.json` is the matching experiment card. Replace its corpus and
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
sec-qwen train config.toml
sec-qwen evaluate config.toml \
  --adapter ../../../artifacts/sec-qwen-v1/adapter \
  --split test-future.jsonl \
  --predictions ../../../artifacts/sec-qwen-v1/test-future.predictions.jsonl
```

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
