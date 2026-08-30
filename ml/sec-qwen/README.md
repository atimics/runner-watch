# SEC Qwen training image

This package trains the FERAL-7B LoRA adapter on a frozen Runner Watch SEC corpus. It accepts only a full
Hugging Face revision and verifies every corpus file against the ilXyr release manifest before it
loads a model.

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
sec-qwen train config.toml
sec-qwen evaluate config.toml \
  --adapter artifacts/sec-qwen-v1/adapter \
  --split test-future.jsonl \
  --predictions artifacts/sec-qwen-v1/test-future.predictions.jsonl
```

`evaluate` prints one strict `{"metrics": {...}}` object with JSON validity, field exactness, and
whole-example exactness. `train` writes a deterministic `adapter.tar`, its SHA-256, the exact model
revision, corpus manifest hash, config hash, seed, and dependency versions. A cloud wrapper uploads
that tar to versioned object storage and puts its URI and provider version into the ilXyr OCI
completion record. Use `sec-qwen completion` to build the provider-neutral completion JSON after
the upload; ilXyr still requires the configured executor to sign the completed run digest.

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
