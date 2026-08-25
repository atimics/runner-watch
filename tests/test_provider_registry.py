from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from runner_watch.provider_contracts import (
    Bar,
    DataKind,
    FetchBatch,
    ProviderProvenance,
    ProviderRequest,
)
from runner_watch.provider_registry import ProviderRegistry, ProvidersExhaustedError


NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)


class FakeBarProvider:
    capabilities = frozenset({DataKind.BARS})

    def __init__(self, name: str, outcome: FetchBatch | Exception) -> None:
        self.name = name
        self.outcome = outcome
        self.calls = 0

    def fetch(self, request: ProviderRequest, progress=None) -> FetchBatch:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome.model_copy(update={"request": request})


def request() -> ProviderRequest:
    return ProviderRequest(kind=DataKind.BARS, symbols=["pen"], interval="5m")


def batch(provider: str, *, status: str = "success") -> FetchBatch:
    requested = request()
    bars = (
        Bar(
            symbol="PEN",
            interval="5m",
            timestamp=NOW,
            open=1,
            high=1.2,
            low=0.9,
            close=1.1,
            volume=1000,
        ),
    )
    return FetchBatch(
        request=requested,
        status=status,
        provenance=ProviderProvenance(
            provider=provider,
            feed="market_bars",
            locator=f"test://{provider}",
            as_of=NOW,
            collected_at=NOW,
        ),
        bars=bars,
    )


def test_registry_uses_explicit_fallback_and_records_attempts() -> None:
    primary = FakeBarProvider("primary", RuntimeError("rate limited"))
    fallback = FakeBarProvider("fallback", batch("fallback"))
    registry = ProviderRegistry()
    registry.register(primary)
    registry.register(fallback)
    registry.route(DataKind.BARS, "primary", "fallback")

    result = registry.fetch(request())

    assert result.provenance.provider == "fallback"
    assert result.provenance.attempted_providers == ("primary", "fallback")
    assert result.provenance.fallback_used is True
    assert primary.calls == fallback.calls == 1


def test_registry_does_not_silently_merge_partial_provider_data() -> None:
    partial = FakeBarProvider("primary", batch("primary", status="partial"))
    fallback = FakeBarProvider("fallback", batch("fallback"))
    registry = ProviderRegistry()
    registry.register(partial)
    registry.register(fallback)
    registry.route(DataKind.BARS, "primary", "fallback")

    result = registry.fetch(
        ProviderRequest(kind=DataKind.BARS, symbols=["PEN", "MISS"], interval="5m")
    )

    assert result.status == "partial"
    assert result.provenance.provider == "primary"
    assert result.provenance.fallback_used is False
    assert fallback.calls == 0


def test_registry_reports_every_failed_provider() -> None:
    registry = ProviderRegistry()
    registry.register(FakeBarProvider("one", RuntimeError("offline")))
    registry.register(FakeBarProvider("two", RuntimeError("bad key")))
    registry.route(DataKind.BARS, "one", "two")

    with pytest.raises(ProvidersExhaustedError) as error:
        registry.fetch(request())

    assert error.value.attempts == (("one", "offline"), ("two", "bad key"))


def test_canonical_contract_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        Bar(symbol="PEN", interval="5m", timestamp=datetime(2026, 8, 25))
