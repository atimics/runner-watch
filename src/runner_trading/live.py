"""Bounded Jupiter execution for a dedicated, operator-funded SPL wallet.

The accepted v6 layouts come from jup-ag/jupiter-cpi/idl.json. New instruction
layouts require a reviewed parser change. A saved signature owns its reservation
until finalized chain evidence or finalized block-height expiry resolves it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import struct
from datetime import datetime

from solders.address_lookup_table_account import AddressLookupTable
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from runner_trading import ledger
from runner_trading.config import USDC, Policy, stamp
from runner_trading.providers import Jupiter, ProviderError, Quote, SolanaRPC, positive_int
from runner_web.solana_keys import ASSOCIATED_TOKEN_PROGRAM, TOKEN_PROGRAM, token_account

JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
COMPUTE = "ComputeBudget111111111111111111111111111111"
SYSTEM = "11111111111111111111111111111111"
TABLE = "AddressLookupTab1e1111111111111111111111111"
ROUTE = hashlib.sha256(b"global:route").digest()[:8]
SHARED = hashlib.sha256(b"global:shared_accounts_route").digest()[:8]
# Byte lengths of enum fields in Jupiter's published v6 Swap layout (0..38).
SWAP_FIELDS = {i: 0 for i in range(39)}
SWAP_FIELDS.update(dict.fromkeys((8, 12, 15, 16, 17, 18, 21, 23, 24, 27, 28), 1))
SWAP_FIELDS.update({29: 16, 33: 4})


def load_signer() -> Keypair:
    if os.getenv("TRADING_LIVE_ENABLED") != "1" or os.getenv("TRADING_MODE") != "live":
        raise ValueError("Enable the dedicated live process before loading a signer")
    path = os.getenv("TRADING_KEY_FILE", "")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("The signer file must be owned by this user with mode 0600")
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise ValueError("Signer file exceeds the size limit")
        signer = Keypair.from_bytes(bytes(json.loads(raw)))
    finally:
        os.close(fd)
    if str(signer.pubkey()) != os.getenv("TRADING_WALLET"):
        raise ValueError("The signer must match TRADING_WALLET")
    return signer


def raw_account(account: dict | None) -> bytes:
    try:
        if not account or account["data"][1] != "base64":
            raise ValueError
        return base64.b64decode(account["data"][0], validate=True)
    except (ValueError, KeyError, TypeError):
        raise ProviderError("Read the complete Solana account") from None


def token_balance(account: dict | None, mint: str, wallet: str) -> int:
    if account is None:
        return 0
    raw = raw_account(account)
    if (
        account.get("owner") != TOKEN_PROGRAM
        or len(raw) != 165
        or str(Pubkey.from_bytes(raw[:32])) != mint
        or str(Pubkey.from_bytes(raw[32:64])) != wallet
        or raw[108] != 1
        or any(raw[72:76])
        or any(raw[129:133])
    ):
        raise ProviderError("A plain, initialized SPL account owned by the trader is required")
    return int.from_bytes(raw[64:72], "little")


def route_amounts(data: bytes) -> tuple[int, int, int, int]:
    if data[:8] not in {ROUTE, SHARED}:
        raise ProviderError("This Jupiter instruction needs a reviewed layout")
    offset = 9 if data[:8] == SHARED else 8
    if len(data) < offset + 4:
        raise ProviderError("Jupiter route data is incomplete")
    count = int.from_bytes(data[offset : offset + 4], "little")
    if not 1 <= count <= 32:
        raise ProviderError("Jupiter route length exceeds the policy")
    offset += 4
    for _ in range(count):
        if offset >= len(data) or data[offset] not in SWAP_FIELDS:
            raise ProviderError("This swap variant needs a reviewed layout")
        offset += 1 + SWAP_FIELDS[data[offset]] + 3
    if len(data) != offset + 19:
        raise ProviderError("Jupiter route has an unexpected data layout")
    return struct.unpack("<QQHB", data[offset:])


class Live:
    def __init__(self, policy: Policy, provider: Jupiter, *, signer=None, rpc=None):
        self.policy = policy
        self.provider = provider
        self.signer = signer or load_signer()
        self.wallet = str(self.signer.pubkey())
        self.rpc = rpc or SolanaRPC()

    def accounts(self, addresses: list[str]) -> list[dict | None]:
        if len(addresses) > 100:
            raise ProviderError("Transaction account count exceeds the limit")
        result = self.rpc(
            "getMultipleAccounts", [addresses, {"encoding": "base64", "commitment": "confirmed"}]
        )
        values = result.get("value")
        if not isinstance(values, list) or len(values) != len(addresses):
            raise ProviderError("Solana account response is incomplete")
        return values

    def validate(self, quote: Quote) -> VersionedTransaction:
        try:
            transaction = VersionedTransaction.from_bytes(
                base64.b64decode(quote.raw["transaction"], validate=True)
            )
            transaction.sanitize()
        except Exception:
            raise ProviderError("Read a complete version 0 transaction") from None
        msg = transaction.message
        if (
            not isinstance(msg, MessageV0)
            or msg.header.num_required_signatures != 1
            or str(msg.account_keys[0]) != self.wallet
            or transaction.uses_durable_nonce()
        ):
            raise ProviderError("The trader must be the sole signer and fee payer")
        keys = [str(k) for k in msg.account_keys]
        writable, readonly = [], []
        for lookup in msg.address_table_lookups:
            item = self.accounts([str(lookup.account_key)])[0]
            if not item or item["owner"] != TABLE:
                raise ProviderError("Read the transaction address table")
            try:
                addresses = AddressLookupTable.deserialize(raw_account(item)).addresses
                writable += [str(addresses[i]) for i in lookup.writable_indexes]
                readonly += [str(addresses[i]) for i in lookup.readonly_indexes]
            except Exception:
                raise ProviderError("Transaction address table is invalid") from None
        keys += writable + readonly
        source = token_account(self.wallet, quote.input_mint, TOKEN_PROGRAM)
        destination = token_account(self.wallet, quote.output_mint, TOKEN_PROGRAM)
        swap_count = 0
        compute = {}
        for instruction in msg.instructions:
            program = keys[instruction.program_id_index]
            args = [keys[i] for i in instruction.accounts]
            data = bytes(instruction.data)
            if program == JUPITER:
                swap_count += 1
                amount, output, slippage, _ = route_amounts(data)
                if (
                    amount != quote.input_raw
                    or output != quote.output_raw
                    or slippage > self.policy.slippage_bps
                    or output * (10_000 - slippage) // 10_000 < quote.minimum_raw
                ):
                    raise ProviderError("Signed route amounts differ from the checked quote")
                if data[:8] == SHARED:
                    expected = {
                        0: TOKEN_PROGRAM,
                        2: self.wallet,
                        3: source,
                        6: destination,
                        7: quote.input_mint,
                        8: quote.output_mint,
                    }
                else:
                    expected = {
                        0: TOKEN_PROGRAM,
                        1: self.wallet,
                        2: source,
                        3: destination,
                        5: quote.output_mint,
                    }
                    if len(args) < 7 or args[4] not in {destination, JUPITER}:
                        raise ProviderError("The output account must belong to the trader")
                if any(i >= len(args) or args[i] != value for i, value in expected.items()):
                    raise ProviderError("Signed route accounts differ from the checked order")
            elif program == ASSOCIATED_TOKEN_PROGRAM:
                if (
                    data not in {b"", b"\x01"}
                    or len(args) < 6
                    or args[0] != self.wallet
                    or args[2] != self.wallet
                    or args[4:6] != [SYSTEM, TOKEN_PROGRAM]
                    or (args[1], args[3])
                    not in {(source, quote.input_mint), (destination, quote.output_mint)}
                ):
                    raise ProviderError("Associated account instruction exceeds the order scope")
            elif program == COMPUTE:
                if not data or (data[0], len(data)) not in {(2, 5), (3, 9)} or args:
                    raise ProviderError("Compute instruction needs a reviewed layout")
                if data[0] in compute:
                    raise ProviderError("Use one compute instruction of each kind")
                compute[data[0]] = int.from_bytes(data[1:], "little")
            else:
                raise ProviderError("Transaction program exceeds the order scope")
        if swap_count != 1:
            raise ProviderError("One checked swap per transaction is required")
        units = compute.get(2, min(1_400_000, 200_000 * len(msg.instructions)))
        priority_fee = (units * compute.get(3, 0) + 999_999) // 1_000_000
        if not 1 <= units <= 1_400_000 or priority_fee + 5000 > self.policy.max_fee_lamports:
            raise ProviderError("The encoded compute fee exceeds the account limit")
        # Fresh mint controls also ensure transfer fees/hooks cannot change the
        # expected input and output between simulation and landing.
        for mint, account in zip(
            (quote.input_mint, quote.output_mint),
            self.accounts([quote.input_mint, quote.output_mint]),
            strict=True,
        ):
            raw = raw_account(account)
            if account["owner"] != TOKEN_PROGRAM or len(raw) != 82 or raw[45] != 1:
                raise ProviderError("Live trading supports plain SPL mints")
            if mint != USDC and (any(raw[:4]) or any(raw[46:50])):
                raise ProviderError("Mint and freeze controls must be revoked")
        accounts = self.accounts(keys)
        for address, account in zip(keys, accounts, strict=True):
            if account and account.get("owner") == TOKEN_PROGRAM:
                raw = raw_account(account)
                if len(raw) == 165 and str(Pubkey.from_bytes(raw[32:64])) == self.wallet:
                    if address not in {source, destination}:
                        raise ProviderError("Use only the order's two trader token accounts")
        watched = [self.wallet, source, destination]
        before = self.accounts(watched)
        simulation = self.rpc(
            "simulateTransaction",
            [
                quote.raw["transaction"],
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": False,
                    "commitment": "confirmed",
                    "accounts": {"encoding": "base64", "addresses": watched},
                },
            ],
        )["value"]
        after = simulation.get("accounts")
        if simulation.get("err") or not after or len(after) != 3:
            raise ProviderError("The swap simulation must succeed")
        debit = token_balance(before[1], quote.input_mint, self.wallet) - token_balance(
            after[1], quote.input_mint, self.wallet
        )
        credit = token_balance(after[2], quote.output_mint, self.wallet) - token_balance(
            before[2], quote.output_mint, self.wallet
        )
        native_debit = before[0]["lamports"] - after[0]["lamports"]
        if (
            debit != quote.input_raw
            or credit < quote.minimum_raw
            or not 0 <= native_debit <= self.policy.max_fee_lamports
        ):
            raise ProviderError("Simulated balance changes exceed the order limits")
        if after[0].get("owner") != SYSTEM or after[0].get("data") != before[0].get("data"):
            raise ProviderError("The trader account must retain its system owner and data")
        return transaction

    def prepare(self, identifier: str, token: str, order: dict, quote: Quote, at: datetime) -> None:
        transaction = self.validate(quote)
        quote.require_fresh(self.policy, self.provider.clock())
        height = positive_int(quote.raw.get("lastValidBlockHeight"))
        current = self.rpc("getBlockHeight", [{"commitment": "confirmed"}])
        if not current < height <= current + 200:
            raise ProviderError("Use a current block-height expiry")
        signed = VersionedTransaction.populate(
            transaction.message, [self.signer.sign_message(to_bytes_versioned(transaction.message))]
        )
        with ledger.locked(identifier, token, self.provider.clock()) as db:
            acc = db.execute(
                "SELECT paused FROM trading_accounts WHERE id=?", (identifier,)
            ).fetchone()
            if order["side"] == "buy" and acc["paused"]:
                raise ProviderError("Entries paused")
            db.execute(
                "UPDATE trading_orders SET status='prepared',quote_json=?,signature=?,"
                "signed_transaction=?,last_valid_height=?,updated_at=? "
                "WHERE id=? AND status='queued'",
                (
                    ledger.encode(
                        {**quote.receipt(), "sol_price_usdc": self.provider.sol_price_usdc()}
                    ),
                    str(signed.signatures[0]),
                    base64.b64encode(bytes(signed)).decode(),
                    height,
                    stamp(at),
                    order["id"],
                ),
            )

    def recover(self, identifier: str, token: str, order: dict, at: datetime) -> None:
        # Observe finalized expiry before asking for absence. This closes the
        # race where an order lands while the expiry check is in flight.
        finalized_slot = self.rpc("getSlot", [{"commitment": "finalized"}])
        height = self.rpc(
            "getBlockHeight", [{"commitment": "finalized", "minContextSlot": finalized_slot}]
        )
        statuses = self.rpc(
            "getSignatureStatuses", [[order["signature"]], {"searchTransactionHistory": True}]
        )
        if statuses.get("context", {}).get("slot", -1) < finalized_slot:
            raise ProviderError("Wait for a consistent finalized chain read")
        status = statuses["value"][0]
        receipt = self.rpc(
            "getTransaction",
            [
                order["signature"],
                {
                    "encoding": "json",
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        if receipt:
            self.settle(identifier, token, order, receipt, at)
            return
        if status is None and height > order["last_valid_height"]:
            ledger.fail_order(
                identifier, token, order["id"], "Transaction expired before landing", at
            )
            return
        if status is not None:
            return
        # Only the stored bytes are retried. A missing response leaves this same
        # signature pending. The reservation stays held throughout recovery.
        with ledger.locked(identifier, token, at) as db:
            db.execute(
                "UPDATE trading_orders SET status='checking',attempts=attempts+1,updated_at=? "
                "WHERE id=?",
                (stamp(at), order["id"]),
            )
        try:
            self.provider.request(
                "execute",
                {
                    "signedTransaction": order["signed_transaction"],
                    "requestId": json.loads(order["quote_json"])["request_id"],
                    "lastValidBlockHeight": str(order["last_valid_height"]),
                },
            )
        except ProviderError:
            return

    def settle(self, identifier: str, token: str, order: dict, receipt: dict, at: datetime) -> None:
        meta = receipt["meta"]
        quote = json.loads(order["quote_json"])
        signatures = receipt["transaction"]["signatures"]
        keys = receipt["transaction"]["message"]["accountKeys"]
        if signatures[0] != order["signature"] or keys[0] != self.wallet:
            raise ProviderError("Read the saved signature's finalized transaction")
        fees = meta["preBalances"][0] - meta["postBalances"][0]
        gas = (fees * quote["sol_price_usdc"] + 999_999_999) // 1_000_000_000
        summary = {
            "signature": order["signature"],
            "slot": receipt["slot"],
            "finalized": True,
            "fee_lamports": fees,
            "gas_usdc": gas,
            "receipt_hash": ledger.key(ledger.encode(receipt)),
        }
        if not 0 <= fees <= self.policy.max_fee_lamports:
            self.review(identifier, token, order, summary, "Review the finalized network cost", at)
            return
        if meta["err"]:
            ledger.fail_order(
                identifier,
                token,
                order["id"],
                "Finalized transaction failed",
                at,
                fee_lamports=fees,
                gas_usdc=gas,
                receipt=summary,
            )
            return

        def balances(name):
            values = {}
            for item in meta.get(name) or []:
                if item.get("owner") == self.wallet:
                    values[item["mint"]] = values.get(item["mint"], 0) + int(
                        item["uiTokenAmount"]["amount"]
                    )
            return values

        before, after = balances("preTokenBalances"), balances("postTokenBalances")
        changes = {
            mint: after.get(mint, 0) - before.get(mint, 0) for mint in before.keys() | after.keys()
        }
        debit = -changes.pop(quote["input_mint"], 0)
        credit = changes.pop(quote["output_mint"], 0)
        if (
            debit != int(order["input_raw"])
            or credit < int(quote["minimum_raw"])
            or any(changes.values())
        ):
            self.review(
                identifier,
                token,
                order,
                {
                    **summary,
                    "debit_raw": str(debit),
                    "credit_raw": str(credit),
                    "other_changes": changes,
                },
                "Review finalized token balance changes",
                at,
            )
            return
        ledger.fill(
            identifier,
            token,
            order["id"],
            input_raw=debit,
            output_raw=credit,
            fee_lamports=fees,
            gas_usdc=gas,
            receipt=summary,
            at=at,
        )

    def review(self, identifier, token, order, receipt, reason, at):
        with ledger.locked(identifier, token, at) as db:
            acc = db.execute(
                "SELECT paused,pause_reason FROM trading_accounts WHERE id=?", (identifier,)
            ).fetchone()
            db.execute(
                "UPDATE trading_orders SET status='checking',result_json=?,updated_at=? WHERE id=?",
                (
                    ledger.encode({**receipt, "review_required": True, "reason": reason}),
                    stamp(at),
                    order["id"],
                ),
            )
            db.execute(
                "UPDATE trading_accounts SET paused=1,pause_reason=? WHERE id=?",
                (reason, identifier),
            )
            if not acc["paused"] or acc["pause_reason"] != reason:
                ledger.event(
                    db,
                    identifier,
                    "receipt_review",
                    {"reason": reason, "order_id": order["id"]},
                    at,
                )

    def reconcile(self, identifier: str, token: str, at: datetime) -> None:
        if any(
            o["status"] != "queued" for o in ledger.rows("trading_orders", identifier, active=True)
        ):
            return
        acc = ledger.account(identifier)
        amounts = {}
        result = self.rpc(
            "getTokenAccountsByOwner",
            [
                self.wallet,
                {"programId": TOKEN_PROGRAM},
                {"encoding": "jsonParsed", "commitment": "finalized"},
            ],
        )
        for item in result["value"]:
            info = item["account"]["data"]["parsed"]["info"]
            amounts[info["mint"]] = amounts.get(info["mint"], 0) + int(
                info["tokenAmount"]["amount"]
            )
        expected = {USDC: acc["cash_usdc"]}
        expected.update(
            {
                p["mint"]: int(p["quantity_raw"])
                for p in ledger.rows("trading_positions", identifier, active=True)
            }
        )
        sol = self.rpc("getBalance", [self.wallet, {"commitment": "finalized"}])["value"]
        if sol != acc["sol_lamports"] or any(
            amounts.get(mint, 0) != amount for mint, amount in expected.items()
        ):
            if not acc["paused"]:
                ledger.pause(
                    identifier, "Match the dedicated wallet to the saved account balances", at
                )
