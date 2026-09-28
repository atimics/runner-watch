"""The rules as data: rules.toml, which the standards read their thresholds from."""

from __future__ import annotations

import tomllib
from functools import cache
from importlib.resources import files
from typing import Any


@cache
def document() -> dict[str, Any]:
    return tomllib.loads(files("ratitrust").joinpath("rules.toml").read_text())


def asset(name: str) -> dict[str, Any]:
    return document()["assets"][name]


def labels(name: str) -> dict[str, str]:
    """Each standard's label by id, in the order the rules list them."""

    return {standard["id"]: standard["label"] for standard in asset(name)["standards"]}


def standard(name: str, standard_id: str) -> dict[str, Any]:
    for entry in asset(name)["standards"]:
        if entry["id"] == standard_id:
            return entry
    raise KeyError(f"{name} has no standard {standard_id}")


def clause(name: str, standard_id: str, fact: str) -> dict[str, Any]:
    """The test clause a standard applies to a fact."""

    for item in standard(name, standard_id)["test"]:
        if item["fact"] == fact:
            return item
    raise KeyError(f"{name}.{standard_id} has no test on {fact}")


def value(name: str, standard_id: str, fact: str) -> Any:
    return clause(name, standard_id, fact)["value"]


def listed(name: str, list_name: str) -> dict[str, str]:
    """A named list as {id: label}."""

    return {item["id"]: item["label"] for item in asset(name)["lists"][list_name]}
