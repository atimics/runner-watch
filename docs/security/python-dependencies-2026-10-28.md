# Python dependency review

Reviewed October 28, 2026 from main `ca1d8c0`.

## Package updates

- The SEC training lock uses certifi 2026.10.23. This includes the certificate
  removals for [GHSA-6q48-6c7w-9gw2](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2).
- Both Python locks use setuptools 84.2.1. Both build manifests require a
  release that includes the fix for
  [GHSA-xmj3-9gf7-h52w](https://github.com/pypa/setuptools/security/advisories/GHSA-xmj3-9gf7-h52w).
- The SEC training lock uses filelock 4.2.4. OSV lists 4.2.1 as the fixed
  version for CVE-2026-112907, CVE-2026-112913 and CVE-2026-112940.
- The SEC training project uses the current Accelerate release, 1.15.0.

## Accelerate checkpoint source review

The [Accelerate advisory](https://osv.dev/vulnerability/PYSEC-2026-3804)
records affected releases through 1.14.0. A version scan of 1.15.0 is clear.
The published 1.15.0 source still joins the checkpoint folder with each
untrusted shard path in `weight_map`. That code needs path validation and
regular-file checks. Keep checkpoint path validation as an open finding when
reviewing a model.

The relevant code is in
[`load_checkpoint_in_model`](https://github.com/huggingface/accelerate/blob/v1.15.0/src/accelerate/utils/modeling.py).
The current training examples pin Qwen to a full model commit. Keep that
revision review as part of selecting model files. A future dependency update
needs a source check for shard paths and special files, alongside the version
scan. The audit uses its full advisory list.

## Validation

- Both lock files passed `uv lock --check`.
- Both Python packages built with the updated build dependency.
- All 18 SEC training and corpus tests passed.
- A tiny offline CPU LoRA job passed training, adapter save, reload and prediction
  with the final locked dependencies.
- The main and SEC training requirement scans reported zero known findings.
  The Accelerate source finding above remains part of this review.
