from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol

import keyring
from keyring.errors import KeyringError

SERVICE_NAME = "chat.rati.scanner"
PROVIDER_ENVIRONMENTS = {
    "openrouter": "OPENROUTER_API_KEY",
    "massive": "MASSIVE_API_KEY",
    "fintel": "FINTEL_API_KEY",
    "the-odds-api": "ODDS_API_KEY",
}


class CredentialVault(Protocol):
    def get(self, provider: str) -> str | None: ...

    def set(self, provider: str, secret: str) -> None: ...

    def delete(self, provider: str) -> bool: ...


class MemoryCredentialVault:
    """Small test and embedding vault; it never persists secrets."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, provider: str) -> str | None:
        return self._values.get(provider)

    def set(self, provider: str, secret: str) -> None:
        self._values[provider] = secret

    def delete(self, provider: str) -> bool:
        return self._values.pop(provider, None) is not None


class EnvironmentCredentialVault:
    """Read-only credentials supplied by a process manager or secret store."""

    def get(self, provider: str) -> str | None:
        variable = PROVIDER_ENVIRONMENTS.get(provider)
        if variable is None:
            return None
        return os.getenv(variable, "").strip() or None

    def set(self, provider: str, secret: str) -> None:
        raise RuntimeError("Environment-managed credentials cannot be changed here")

    def delete(self, provider: str) -> bool:
        return False


class KeyringCredentialVault:
    """Persist local user credentials in the operating system credential vault."""

    def get(self, provider: str) -> str | None:
        try:
            return keyring.get_password(SERVICE_NAME, provider)
        except KeyringError:
            return None

    def set(self, provider: str, secret: str) -> None:
        try:
            keyring.set_password(SERVICE_NAME, provider, secret)
        except KeyringError as exc:
            raise RuntimeError("The operating system credential vault is unavailable") from exc

    def delete(self, provider: str) -> bool:
        try:
            if keyring.get_password(SERVICE_NAME, provider) is None:
                return False
            keyring.delete_password(SERVICE_NAME, provider)
            return True
        except KeyringError:
            return False


class LayeredCredentialVault:
    """Prefer operator-managed environment secrets, then local user credentials."""

    def __init__(self, writable: CredentialVault) -> None:
        self.environment = EnvironmentCredentialVault()
        self.writable = writable

    def get(self, provider: str) -> str | None:
        return self.environment.get(provider) or self.writable.get(provider)

    def set(self, provider: str, secret: str) -> None:
        self.writable.set(provider, secret)

    def delete(self, provider: str) -> bool:
        return self.writable.delete(provider)


def credential_vault(backend: str) -> CredentialVault:
    if backend == "environment":
        return EnvironmentCredentialVault()
    if backend == "memory":
        return LayeredCredentialVault(MemoryCredentialVault())
    if backend == "keyring":
        return LayeredCredentialVault(KeyringCredentialVault())
    raise ValueError("RATI_CREDENTIAL_BACKEND must be environment, keyring, or memory")
