"""Export the frozen research model for the Python shadow scorer."""

import hashlib
import json
from pathlib import Path

import lightgbm as lgb

from runner_web.attention_study import BASE_FEATURES, CONTEXT_FEATURES, TARGET

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/research/attention-study-2026-09-24/attention-candidate.txt"
OUTPUT = ROOT / "src/runner_web/assets/attention-shadow-v1.json"


def node(tree: dict) -> dict:
    if "leaf_value" in tree:
        return {"leaf": tree["leaf_value"]}
    if tree["decision_type"] != "<=":
        raise ValueError("Export expects numeric splits")
    return {
        "feature": tree["split_feature"],
        "threshold": tree["threshold"],
        "default_left": tree["default_left"],
        "missing_type": tree["missing_type"],
        "left": node(tree["left_child"]),
        "right": node(tree["right_child"]),
    }


if __name__ == "__main__":
    model = lgb.Booster(model_file=str(SOURCE)).dump_model()
    if model["objective"] != "binary sigmoid:1" or model["average_output"]:
        raise ValueError("Export expects a binary sum of trees")
    payload = {
        "id": "attention-context-20260924-v1",
        "target": TARGET,
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "features": [*BASE_FEATURES, *CONTEXT_FEATURES],
        "trees": [node(tree["tree_structure"]) for tree in model["tree_info"]],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    print(hashlib.sha256(OUTPUT.read_bytes()).hexdigest())
