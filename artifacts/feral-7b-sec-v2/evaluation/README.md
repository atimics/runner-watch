# FERAL-7B Qwen2.5 FinQA baseline

This directory records the completed base-model FinQA baseline for FERAL-7B.

- Model: `Qwen/Qwen2.5-7B-Instruct`
- Exact model revision: `a09a35458c702b33eeacc393d103063234e8bc28`
- Adapter: none
- FinQA source revision: `0f16e2867befa6840783e58be38c9efb9229d742`
- Evaluation cases: 1,147
- Exact accuracy: 0.14646904969485613
- Device: Apple MPS
- Training authorized: no

The suite uses FinQA's checked-in retrieved `qa.model_input` context. It measures financial
reasoning on the official retrieved context, not end-to-end document retrieval. The manifest,
configuration, predictions, and result record contain the frozen hashes needed to verify the run.

This is a real base-model evaluation result. It is not evidence that a FERAL-7B adapter exists, and
it does not authorize training or paid compute.
