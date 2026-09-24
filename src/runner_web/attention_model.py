"""Frozen binary trees used by the prospective attention trial."""

from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache
from importlib.resources import files

from runner_web.attention_study import BASE_FEATURES, CONTEXT_FEATURES, TARGET

SOURCE_SHA256 = "a904b966dd915112379ab2f6b763ea55afc79216b1351100218386e446864965"
ARTIFACT_SHA256 = "ac6b8446b1ee99208ba4d8352c87d6e283d2db3537e0538d62903136c1e3a497"


@lru_cache(maxsize=1)
def artifact() -> dict:
    data = files("runner_web").joinpath("assets/attention-shadow-v1.json").read_bytes()
    model = json.loads(data)
    if (
        hashlib.sha256(data).hexdigest() != ARTIFACT_SHA256
        or model["source_sha256"] != SOURCE_SHA256
        or model["target"] != TARGET
        or model["features"] != [*BASE_FEATURES, *CONTEXT_FEATURES]
        or len(model["trees"]) != 120
    ):
        raise ValueError("Attention artifact contract mismatch")
    return {**model, "sha256": hashlib.sha256(data).hexdigest()}


def probability(vector: list[float | None]) -> float:
    model = artifact()
    if len(vector) != len(model["features"]):
        raise ValueError("Attention feature count mismatch")
    total = 0.0
    for tree in model["trees"]:
        while "leaf" not in tree:
            value = vector[tree["feature"]]
            missing = value is None or math.isnan(value)
            if tree["missing_type"] == "Zero":
                missing = missing or abs(value or 0) <= 1e-35
            if missing and tree["missing_type"] != "None":
                left = tree["default_left"]
            else:
                left = (0.0 if missing else value) <= tree["threshold"]
            tree = tree["left"] if left else tree["right"]
        total += tree["leaf"]
    return 1.0 / (1.0 + math.exp(-max(-700, min(700, total))))
