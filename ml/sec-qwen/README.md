# SEC Qwen training image

This package trains the FERAL-7B LoRA adapter on a frozen SEC corpus. It accepts only a full
Hugging Face revision and verifies every corpus file against either an ilXyr release manifest or a
content-addressed Braid `braid.release/v2` manifest before it loads a model.

For new FERAL runs, use Braid. Copy `config.braid.example.toml`, replace the pending release ID and
manifest digest with the accepted Braid release values, and point `corpus_manifest` at its
`release.json`. Validation fails closed if the manifest hash, release identity, release status, or
any declared artifact differs. Braid rows must keep the original `stonks.sec_chat_example.v1` or
`v2` object in the row's `metadata`; Braid's `text` field remains available for quality and
deduplication checks. Training consumes only the preserved chat messages from the verified release.

The Braid release should expose `data/train.jsonl` and `data/validation.jsonl`. Keep future and
unseen-issuer tests in the frozen ilXyr evaluation release rather than placing them in a training
configuration. Accepting a Braid release does not authorize training.

Use a pinned GPU base image. The Dockerfile intentionally has no mutable default:

```bash
docker build ml/sec-qwen \
  --build-arg BASE_IMAGE=YOUR_GPU_PYTHON_IMAGE@sha256:FULL_DIGEST \
  --build-arg MODEL_ID=Qwen/Qwen2.5-7B-Instruct \
  --build-arg MODEL_REVISION=a09a35458c702b33eeacc393d103063234e8bc28 \
  -t sec-qwen:local
```

The build stores that exact model snapshot inside the image. The running image forces Hugging Face
and Transformers offline, so an ilXyr job with denied network access cannot drift to new weights.

Run the same image locally, in SageMaker, Azure ML, or as a Kubernetes Job:

```bash
sec-qwen validate config.toml
sec-qwen profile config.toml \
  --tokens-per-gpu-hour MEASURED_CALIBRATION_THROUGHPUT \
  --gpu-hour-price CURRENT_PROVIDER_PRICE \
  --output artifacts/feral-7b-sec-v2/profile.json
sec-qwen calibrate config.toml \
  --sample-fraction 0.01 \
  --output artifacts/feral-7b-sec-v2/calibration
sec-qwen train config.toml
sec-qwen evaluate config.toml \
  --adapter artifacts/feral-7b-sec-v2/adapter \
  --split test-future.jsonl \
  --predictions artifacts/feral-7b-sec-v2/test-future.predictions.jsonl
```

`calibrate` deterministically selects one percent of training examples by example-ID hash, runs the
real LoRA training path without saving an adapter, and records measured effective-token throughput.
Its receipt keeps `training_authorized` false. Use the same image and GPU planned for the full run
before turning that throughput into a provider cost limit.

Omit `--adapter` to measure the pinned base-model baseline on the same frozen split. The command
uses the identical tokenizer, prompt construction, batching, generation settings, and scorer for
base and candidate runs.

Use `prepare-finqa`, `prepare-citation-support`, and `benchmark-evaluate` to freeze and run the
FinQA and confident-unsupported-answer baselines described in the repository training guide. The
prepared suites include digest-bearing manifests and use identical IDs for base and candidate runs.

`evaluate` prints one strict `{"metrics": {...}}` object with JSON validity, field exactness, and
whole-example exactness. `train` writes a deterministic `adapter.tar`, its SHA-256, the exact model
revision, corpus manifest hash, config hash, seed, and dependency versions. A cloud wrapper uploads
that tar to versioned object storage and puts its URI and provider version into the ilXyr OCI
completion record. Use `sec-qwen completion` to build the provider-neutral completion JSON after
the upload; ilXyr still requires the configured executor to sign the completed run digest.

`profile` loads only the pinned tokenizer. It reports exact effective and supervised tokens,
truncation, tokens by task, optimizer steps, and an optional cost ceiling before model weights are
loaded. The example uses four data-loader workers and batched greedy evaluation; these settings stay
inside the hashed config for replay.

The release gate mirrors the FERAL-7B experiment card: improve FinQA by at least 8 percentage
points, do not increase confident hallucinations, and retain at least 70% exact SEC fields. Supply
candidate and base-model result files for the same sealed IDs:

```bash
sec-qwen release-score \
  --sec-predictions test-future.predictions.jsonl \
  --candidate-finqa candidate-finqa.jsonl \
  --baseline-finqa baseline-finqa.jsonl \
  --candidate-hallucination candidate-hallucination.jsonl \
  --baseline-hallucination baseline-hallucination.jsonl
```

The hallucination rows use deterministic support labels and a recorded model confidence. They do
not use an unversioned model judge.
