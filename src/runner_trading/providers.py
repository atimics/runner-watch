from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from runner_trading.config import SOL, USDC, Policy, age, number, stamp


class ProviderError(ValueError):
    pass


def positive_int(value: Any) -> int:
    if isinstance(value, bool) or not str(value).isdigit():
        raise ProviderError("Provider amount is invalid")
    result = int(value)
    if result <= 0 or result > 2**64 - 1:
        raise ProviderError("Provider amount is outside the supported range")
    return result


def http_json(url: str, body: dict | None = None, headers: dict | None = None) -> dict:
    request = Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "User-Agent": "RATi-Trader/1",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urlopen(request, timeout=8) as reply:
            data = reply.read(2_000_001)
        if len(data) > 2_000_000:
            raise ProviderError("Provider response exceeds the size limit")
        result = json.loads(data)
    except Exception:
        # Provider URLs can contain credentials. Persist a bounded public reason.
        raise ProviderError("Provider request failed; retry is scheduled") from None
    if not isinstance(result, dict):
        raise ProviderError("Provider response must be an object")
    return result


@dataclass(frozen=True)
class Quote:
    input_mint: str
    output_mint: str
    input_raw: int
    output_raw: int
    minimum_raw: int
    fee_lamports: int
    impact_bps: int
    observed_at: str
    raw: dict[str, Any]

    def receipt(self) -> dict[str, Any]:
        return {
            "input_mint": self.input_mint,
            "output_mint": self.output_mint,
            "input_raw": str(self.input_raw),
            "output_raw": str(self.output_raw),
            "minimum_raw": str(self.minimum_raw),
            "fee_lamports": self.fee_lamports,
            "impact_bps": self.impact_bps,
            "observed_at": self.observed_at,
            "router": self.raw.get("router"),
            "request_id": self.raw.get("requestId"),
            "fee_bps": self.raw.get("feeBps"),
            "fee_mint": self.raw.get("feeMint"),
        }

    def require_fresh(self, policy: Policy, at: datetime) -> None:
        if not 0 <= age(self.observed_at, at) <= policy.quote_max_age_seconds:
            raise ProviderError("Swap quote expired")
        expiry = self.raw.get("expireAt")
        if expiry and age(expiry, at) >= 0:
            raise ProviderError("Swap route expired")


class Jupiter:
    def __init__(self, policy: Policy, *, transport=http_json, clock=None, pace: bool = True):
        self.policy = policy
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(UTC))
        self.api_key = os.getenv("JUPITER_API_KEY", "")
        self.pace = pace
        self._last_request = 0.0
        self._sol_quote: tuple[float, int] | None = None

    def request(self, path: str, body: dict | None = None) -> dict:
        interval = 0.2 if self.api_key else 2.1
        if self.pace:
            time.sleep(max(0, interval - (time.monotonic() - self._last_request)))
        self._last_request = time.monotonic()
        return self.transport(
            "https://api.jup.ag/swap/v2/" + path,
            body,
            {"x-api-key": self.api_key} if self.api_key else {},
        )

    def quote(
        self, input_mint: str, output_mint: str, amount: int, *, taker: str | None = None
    ) -> Quote:
        amount = positive_int(amount)
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(self.policy.slippage_bps),
            "excludeRouters": "jupiterz,dflow,okx",
            "maxSupportedTransactionVersion": "0",
        }
        if taker:
            params["taker"] = taker
        raw = self.request("order?" + urlencode(params))
        if raw.get("errorCode") or raw.get("error"):
            raise ProviderError("A swap route is unavailable")
        if (
            raw.get("inputMint") != input_mint
            or raw.get("outputMint") != output_mint
            or positive_int(raw.get("inAmount")) != amount
            or raw.get("swapMode") != "ExactIn"
            or raw.get("router") != "metis"
        ):
            raise ProviderError("Swap route differs from the order")
        output = positive_int(raw.get("outAmount"))
        minimum = positive_int(raw.get("otherAmountThreshold"))
        floor = output * (10_000 - self.policy.slippage_bps) // 10_000
        if minimum > output or minimum < floor:
            raise ProviderError("Swap minimum exceeds the slippage policy")
        impact = (
            abs(number(raw["priceImpact"])) * 100
            if "priceImpact" in raw
            else abs(number(raw.get("priceImpactPct"))) * 10_000
        )
        if input_mint == USDC and impact > self.policy.max_price_impact_bps:
            raise ProviderError("Swap price impact exceeds the limit")
        fees = 0
        for name in ("signatureFeeLamports", "prioritizationFeeLamports", "rentFeeLamports"):
            fee = number(raw.get(name, 0))
            if fee < 0 or fee != int(fee):
                raise ProviderError("Swap fee is invalid")
            fees += int(fee)
        fees = max(fees, self.policy.paper_fee_lamports)
        if input_mint == USDC:
            # Reserve a fresh associated account in the paper cost model.
            fees = max(fees, 2_039_280 + self.policy.paper_fee_lamports)
        if fees > self.policy.max_fee_lamports:
            raise ProviderError("Network and account costs exceed the limit")
        return Quote(
            input_mint,
            output_mint,
            amount,
            output,
            minimum,
            fees,
            int(impact.to_integral_value(rounding=ROUND_CEILING)),
            stamp(self.clock()),
            raw,
        )

    def sol_price_usdc(self) -> int:
        if self._sol_quote and time.monotonic() - self._sol_quote[0] < 60:
            return self._sol_quote[1]
        quote = self.quote(SOL, USDC, 1_000_000_000)
        self._sol_quote = (time.monotonic(), quote.output_raw)
        return quote.output_raw

    def gas_cost(self, lamports: int) -> int:
        return (lamports * self.sol_price_usdc() + 999_999_999) // 1_000_000_000

    def paper_output(self, quote: Quote) -> int:
        output = min(quote.minimum_raw, quote.output_raw)
        fee = quote.raw.get("platformFee") or {}
        if quote.raw.get("feeMint") == quote.output_mint:
            # A separate output-fee reserve makes the paper assumption explicit.
            output -= max(0, int(fee.get("amount") or 0))
        output = output * (10_000 - self.policy.paper_haircut_bps) // 10_000
        return positive_int(output)


class SolanaRPC:
    def __init__(self, *, transport=http_json, url: str | None = None):
        self.transport = transport
        self.url = url or os.getenv("TRADING_RPC_URL", "")
        if urlparse(self.url).scheme != "https" or not urlparse(self.url).hostname:
            raise ValueError("Live trading needs a dedicated HTTPS TRADING_RPC_URL")

    def __call__(self, method: str, params: list) -> Any:
        response = self.transport(
            self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if "error" in response or "result" not in response:
            raise ProviderError("Solana read failed")
        return response["result"]
