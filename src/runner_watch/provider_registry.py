from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runner_watch.provider_contracts import (
    DataKind,
    FetchBatch,
    ProviderAdapter,
    ProviderRequest,
)


class ProviderConfigurationError(ValueError):
    pass


class ProvidersExhaustedError(RuntimeError):
    def __init__(self, request: ProviderRequest, attempts: list[tuple[str, str]]) -> None:
        self.request = request
        self.attempts = tuple(attempts)
        detail = "; ".join(f"{provider}: {error}" for provider, error in attempts)
        super().__init__(f"No provider completed the {request.kind.value} request ({detail})")


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    kind: DataKind
    providers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.providers:
            raise ProviderConfigurationError(f"{self.kind.value} needs at least one provider")


class ProviderRegistry:
    """Routes one canonical request through an explicit provider fallback list."""

    def __init__(self) -> None:
        self._providers: dict[str, ProviderAdapter] = {}
        self._routes: dict[DataKind, ProviderRoute] = {}

    def register(self, provider: ProviderAdapter) -> None:
        name = provider.name.strip().lower()
        if not name:
            raise ProviderConfigurationError("A provider must have a name")
        if name in self._providers:
            raise ProviderConfigurationError(f"Provider {name!r} is already registered")
        self._providers[name] = provider

    def route(self, kind: DataKind, *providers: str) -> None:
        names = tuple(name.strip().lower() for name in providers if name.strip())
        for name in names:
            provider = self._providers.get(name)
            if provider is None:
                raise ProviderConfigurationError(f"Provider {name!r} is not registered")
            if kind not in provider.capabilities:
                raise ProviderConfigurationError(
                    f"Provider {name!r} does not support {kind.value}"
                )
        self._routes[kind] = ProviderRoute(kind=kind, providers=names)

    def fetch(self, request: ProviderRequest, progress: Any = None) -> FetchBatch:
        route = self._routes.get(request.kind)
        if route is None:
            raise ProviderConfigurationError(f"No route is configured for {request.kind.value}")

        attempts: list[tuple[str, str]] = []
        attempted_names: list[str] = []
        for name in route.providers:
            attempted_names.append(name)
            provider = self._providers[name]
            try:
                batch = provider.fetch(request, progress=progress)
            except Exception as exc:
                attempts.append((name, str(exc)[:500]))
                continue
            if batch.request != request:
                raise ProviderConfigurationError(
                    f"Provider {name!r} returned a batch for a different request"
                )
            if batch.provenance.provider.strip().lower() != name:
                raise ProviderConfigurationError(
                    f"Provider {name!r} reported provenance for "
                    f"{batch.provenance.provider!r}"
                )
            if batch.status == "error" or batch.item_count == 0:
                attempts.append((name, batch.error or "provider returned no canonical records"))
                continue
            provenance = batch.provenance.model_copy(
                update={
                    "attempted_providers": tuple(attempted_names),
                    "fallback_used": name != route.providers[0],
                    "warnings": tuple(
                        [
                            *(f"{provider} failed: {error}" for provider, error in attempts),
                            *batch.provenance.warnings,
                        ]
                    ),
                    "quality": {
                        **batch.provenance.quality,
                        "provider_failures": [
                            {"provider": provider, "error": error}
                            for provider, error in attempts
                        ],
                    },
                }
            )
            return batch.model_copy(update={"provenance": provenance})

        raise ProvidersExhaustedError(request, attempts)

    def describe(self) -> dict[str, Any]:
        return {
            "providers": {
                name: sorted(kind.value for kind in provider.capabilities)
                for name, provider in sorted(self._providers.items())
            },
            "routes": {
                kind.value: list(route.providers)
                for kind, route in sorted(self._routes.items(), key=lambda item: item[0].value)
            },
        }
