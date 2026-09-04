from __future__ import annotations

import json
import math
import os
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from runner_watch.ingestion import IssuerFact, SourceBatch, SourceFetch
from runner_web.ingestion import record_source_batch

SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "RunnerWatch/0.2 https://stonks.rati.foundation")
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
Download = Callable[[str, float], tuple[bytes, str | None]]

FACT_TAGS: dict[tuple[str, str], str] = {
    ("dei", "EntityCommonStockSharesOutstanding"): "shares_outstanding",
    ("dei", "EntityPublicFloat"): "public_float",
    ("us-gaap", "CommonStockSharesOutstanding"): "shares_outstanding",
    ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"): "cash",
    (
        "us-gaap",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ): "cash",
    ("us-gaap", "AssetsCurrent"): "assets_current",
    ("us-gaap", "LiabilitiesCurrent"): "liabilities_current",
    ("us-gaap", "LongTermDebtCurrent"): "debt_current",
    ("us-gaap", "LongTermDebtNoncurrent"): "debt_noncurrent",
    ("us-gaap", "LongTermDebt"): "debt_total",
    ("us-gaap", "OperatingCashFlow"): "operating_cash_flow",
    (
        "us-gaap",
        "NetCashProvidedByUsedInOperatingActivities",
    ): "operating_cash_flow",
    ("us-gaap", "NetCashUsedInOperatingActivities"): "operating_cash_flow",
    ("us-gaap", "StockholdersEquity"): "stockholders_equity",
}
ALLOWED_FORMS = {"10-K", "10-Q", "20-F", "40-F", "6-K", "8-K"}


def _download(url: str, timeout: float) -> tuple[bytes, str | None]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get_content_type()


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def parse_company_facts(
    payload: dict[str, Any], *, collected_at: datetime | None = None
) -> tuple[IssuerFact, ...]:

    try:
        cik = int(payload["cik"])
    except (KeyError, TypeError, ValueError):
        return ()
    cutoff = (collected_at or datetime.now(UTC)).date() - timedelta(days=3 * 366)
    grouped: dict[str, list[IssuerFact]] = defaultdict(list)
    namespaces = payload.get("facts")
    if not isinstance(namespaces, dict):
        return ()

    for (namespace, source_tag), concept in FACT_TAGS.items():
        raw = namespaces.get(namespace, {}).get(source_tag, {})
        units = raw.get("units") if isinstance(raw, dict) else None
        if not isinstance(units, dict):
            continue
        for unit, entries in units.items():
            if unit not in {"USD", "shares"} or not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or str(entry.get("form") or "") not in ALLOWED_FORMS:
                    continue
                period_end = _day(entry.get("end"))
                filed = _day(entry.get("filed"))
                accession = str(entry.get("accn") or "").strip()
                try:
                    value = float(entry["val"])
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    period_end is None
                    or filed is None
                    or period_end < cutoff
                    or not accession
                    or not math.isfinite(value)
                ):
                    continue
                if source_tag == "NetCashUsedInOperatingActivities" and value > 0:
                    value = -value
                grouped[concept].append(
                    IssuerFact(
                        cik=cik,
                        concept=concept,
                        value=value,
                        unit=unit,
                        period_start=_day(entry.get("start")),
                        period_end=period_end,
                        filed_at=datetime.combine(filed, datetime.min.time(), tzinfo=UTC),
                        accession=accession,
                        form=str(entry.get("form") or "") or None,
                        source_tag=f"{namespace}:{source_tag}",
                        payload={
                            key: entry[key]
                            for key in ("fy", "fp", "frame")
                            if entry.get(key) is not None
                        },
                    )
                )

    output: list[IssuerFact] = []
    seen: set[tuple[Any, ...]] = set()
    for concept, facts in grouped.items():
        facts.sort(key=lambda fact: (fact.filed_at, fact.period_end), reverse=True)
        for fact in facts:
            identity = (
                concept,
                fact.period_start,
                fact.period_end,
                fact.filed_at,
                fact.accession,
                fact.value,
                fact.unit,
            )
            if identity in seen:
                continue
            seen.add(identity)
            output.append(fact)
            if sum(item.concept == concept for item in output) >= 12:
                break
    return tuple(output)


def refresh_company_facts(
    cik: int, *, timeout: float = 20, download: Download = _download
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    locator = COMPANY_FACTS_URL.format(cik=int(cik))
    try:
        body, content_type = download(locator, timeout)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("SEC company facts response is not an object")
        facts = parse_company_facts(payload, collected_at=datetime.now(UTC))
    except Exception as exc:
        run_id = record_source_batch(
            SourceBatch(
                fetch=SourceFetch.failure(
                    source="sec",
                    feed="company_facts",
                    locator=locator,
                    started_at=started_at,
                    error=exc,
                    metadata={"cik": int(cik), "requested_count": 1},
                )
            )
        )
        raise RuntimeError(f"SEC company facts failed in run {run_id}: {exc}") from exc

    fetch = SourceFetch.success(
        source="sec",
        feed="company_facts",
        locator=locator,
        started_at=started_at,
        payload=body,
        content_type=content_type or "application/json",
        metadata={
            "cik": int(cik),
            "requested_count": 1,
            "received_count": len(facts),
        },
    )
    run_id = record_source_batch(SourceBatch(fetch=fetch, issuer_facts=facts))
    return {"run_id": run_id, "cik": int(cik), "facts": len(facts)}
