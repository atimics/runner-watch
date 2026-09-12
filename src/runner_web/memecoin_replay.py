"""Replay saved Solana evidence under a versioned repository quality policy."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from runner_web.helius_discovery import _address
from runner_web.memecoin_chain_parser import parse_events

POLICY = {
    "id": "repository-qc1000/memecoin-replay/1",
    "owner": "runner-watch repository",
    "schema": "memecoin-replay/1",
    "max_events": 128,
    "max_nodes": 48,
    "max_frames": 12,
    "max_receipt_bytes": 4 * 1024 * 1024,
    "max_gif_bytes": 8 * 1024 * 1024,
    "controls": ["receipt_hash", "decoder_replay", "subject_scope", "time_order", "bounded_view"],
}
TIMING = {"transition": 800, "hold": 450, "origin_hold": 950, "final_hold": 1700, "reset": 900}
COLORS = {"launch": "#d3eb86", "wallet": "#77c9ce", "token": "#c8d284", "pool": "#b69be4"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_identity() -> str:
    root = Path(__file__).parent
    return digest(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("memecoin_replay.py", "memecoin_chain_parser.py", "memecoin_replay_gif.py")
        }
    )


def merkle_root(receipts: list[dict]) -> str:
    nodes = [hashlib.sha256(b"\x00" + canonical(r).encode()).digest() for r in receipts]
    if not nodes:
        return hashlib.sha256(b"").hexdigest()
    while len(nodes) > 1:
        if len(nodes) % 2:
            nodes.append(nodes[-1])
        nodes = [
            hashlib.sha256(b"\x01" + nodes[i] + nodes[i + 1]).digest()
            for i in range(0, len(nodes), 2)
        ]
    return nodes[0].hex()


def _order(event: dict) -> tuple:
    return event["slot"], event["signature"], event["event_id"]


def _bounded(items: list, limit: int) -> list:
    if len(items) <= limit:
        return items
    first = limit // 2
    return items[:first] + items[-(limit - first) :]


def _coholdings(tx: dict, mint: str) -> list[dict]:
    """Use positive balances together in one transaction's post-state."""
    owners: dict[str, list] = defaultdict(list)
    for row in tx["meta"].get("postTokenBalances") or []:
        try:
            owner, asset = _address(row["owner"]), _address(row["mint"])
            raw = row["uiTokenAmount"]["amount"]
            decimals = row["uiTokenAmount"]["decimals"]
            if (
                not isinstance(raw, str)
                or not raw.isascii()
                or not raw.isdigit()
                or len(raw) > 30
                or int(raw) <= 0
                or type(decimals) is not int
                or not 0 <= decimals <= 18
            ):
                continue
            owners[owner].append({"mint": asset, "raw": raw, "decimals": decimals})
        except (KeyError, ValueError, TypeError):
            continue
    result = []
    signature = tx["transaction"]["signatures"][0]
    for owner, balances in sorted(owners.items()):
        seed = [balance for balance in balances if balance["mint"] == mint]
        if not seed:
            continue
        for asset in sorted({b["mint"] for b in balances if b["mint"] != mint}):
            result.append(
                {
                    "kind": "shared_holdings",
                    "wallet": owner,
                    "token_address": asset,
                    "signature": signature,
                    "slot": tx["slot"],
                    "event_id": f"{signature}:holdings:{owner}:{asset}",
                    "balances": seed + [b for b in balances if b["mint"] == asset],
                    "amount_basis": "transaction_post_balances",
                }
            )
    return result


def checked_events(receipts: list[dict], mint: str) -> list[dict]:
    """Recompute events from each hashed successful transaction before drawing."""
    events = {}
    for receipt in receipts:
        tx = receipt["transaction"]
        if digest(tx) != receipt["sha256"]:
            raise ValueError("receipt_hash")
        signature = tx["transaction"]["signatures"][0]
        if (
            receipt["signature"] != signature
            or tx["meta"]["err"] is not None
            or type(tx["slot"]) is not int
            or tx["slot"] < 0
        ):
            raise ValueError("decoder_replay")
        decoded = parse_events(tx)
        for event in decoded:
            event = {**event, "receipt_sha256": receipt["sha256"]}
            events[event["event_id"]] = event
        for event in _coholdings(tx, mint):
            event.update(
                observed_at=next((e["observed_at"] for e in decoded), None),
                receipt_sha256=receipt["sha256"],
            )
            events[event["event_id"]] = event
    own = [
        e
        for e in events.values()
        if e.get("token_address") == mint and e["kind"] != "shared_holdings"
    ]
    wallets = {e["wallet"] for e in own}
    launch = next(iter(sorted((e for e in own if e["kind"] == "token_launch"), key=_order)), None)
    scoped = [e for e in events.values() if e in own or e.get("wallet") in wallets]
    if launch:
        scoped = [e for e in scoped if e["slot"] >= launch["slot"]]
    return sorted(scoped, key=_order)


def _relations(event: dict, mint: str) -> list[tuple[str, str, str]]:
    wallet = "wallet:" + event["wallet"]
    asset = (
        "launch"
        if event.get("token_address") == mint
        else "token:" + str(event.get("token_address", ""))
    )
    kind = event["kind"]
    if kind == "sol_transfer":
        return [("wallet:" + event["source_wallet"], wallet, "funded")]
    if kind == "shared_holdings":
        return [
            (wallet, "launch", "held at this transaction"),
            (wallet, asset, "held at this transaction"),
        ]
    if kind == "token_launch":
        links = [(wallet, asset, "launched")]
        if event.get("declared_creator") and event["declared_creator"] != "1" * 32:
            links.append(("wallet:" + event["declared_creator"], asset, "declared creator"))
        return links
    if kind == "pool_created":
        pool = "pool:" + event["pool_address"]
        return [(wallet, pool, "created pool"), (pool, asset, "pool token")]
    if kind == "swap":
        return [(wallet, asset, "bought" if event["direction"] == "buy" else "sold")]
    if kind == "liquidity_withdrawal":
        return [(wallet, asset, "withdrew liquidity")]
    return []


def event_window(all_events: list[dict], mint: str) -> list[dict]:
    events = _bounded(all_events, POLICY["max_events"])
    launch = next(
        (e for e in all_events if e["kind"] == "token_launch" and e["token_address"] == mint), None
    )
    if launch and launch not in events:
        events = sorted([launch, *events[1:]], key=_order)
    return events


def project(events: list[dict], mint: str) -> dict:
    launch = next(
        (e for e in events if e["kind"] == "token_launch" and e["token_address"] == mint), None
    )
    nodes = {
        "launch": {
            "id": "launch",
            "kind": "launch",
            "address": mint,
            "label": "Token launch",
            "x": 400,
            "y": 292,
            "r": 48,
        }
    }
    links = []
    shown_events = []
    for event in events:
        relations = _relations(event, mint)
        additions = {key for a, b, _ in relations for key in (a, b)} - nodes.keys()
        if len(nodes) + len(additions) > POLICY["max_nodes"]:
            continue
        for key in sorted(additions):
            kind, address = key.split(":", 1)
            angle = (len(nodes) * 2.399963229728653) % (math.pi * 2)
            distance = 105 + (len(nodes) % 3) * 56
            nodes[key] = {
                "id": key,
                "kind": kind,
                "address": address,
                "label": address[:4] + "…" + address[-4:],
                "x": round(400 + math.cos(angle) * distance, 3),
                "y": round(292 + math.sin(angle) * distance, 3),
                "r": 18,
                "parent": relations[0][1] if relations[0][1] in nodes else "launch",
            }
        for a, b, role in relations:
            if a == b:
                continue
            links.append(
                {
                    "id": f"{event['event_id']}:{a}:{b}:{role}",
                    "source": a,
                    "target": b,
                    "role": role,
                    "event_id": event["event_id"],
                    "signature": event["signature"],
                    "slot": event["slot"],
                }
            )
        if relations:
            shown_events.append(event)
    slots = sorted({e["slot"] for e in shown_events})
    if len(slots) > POLICY["max_frames"] - 1:
        n = POLICY["max_frames"] - 1
        slots = [slots[round(i * (len(slots) - 1) / (n - 1))] for i in range(n)]
    origin = {
        "nodes": [nodes["launch"]],
        "edges": [],
        "slot": None,
        "label": "Token launch" if launch else "Launch evidence pending",
    }
    frames = [origin]
    for slot in slots:
        edges = [edge for edge in links if edge["slot"] <= slot]
        keys = {"launch"} | {key for edge in edges for key in (edge["source"], edge["target"])}
        frame_nodes = [
            {
                **node,
                "r": node["r"]
                if key == "launch"
                else min(
                    28, 16 + math.sqrt(sum(key in (e["source"], e["target"]) for e in edges)) * 2
                ),
            }
            for key, node in nodes.items()
            if key in keys
        ]
        frames.append(
            {"nodes": frame_nodes, "edges": edges, "slot": slot, "label": f"Through slot {slot:,}"}
        )
    return {
        "launch": launch,
        "frames": frames,
        "drawn_events": len(shown_events),
        "drawn_nodes": len(nodes),
    }


def build_replay(coin: dict, receipts: list[dict], coverage: dict) -> dict:
    mint = _address(coin["token_address"])
    if coin.get("network") != "solana":
        raise ValueError("subject_scope")
    unique = {}
    for receipt in receipts:
        prior = unique.get(receipt["signature"])
        if prior and prior["sha256"] != receipt["sha256"]:
            raise ValueError("conflicting_receipt")
        unique[receipt["signature"]] = receipt
    candidates = sorted(unique.values(), key=lambda r: (r["transaction"]["slot"], r["signature"]))
    selected = []
    size = 0
    # Select both ends before applying the byte budget; every omission is counted.
    for receipt in _bounded(candidates, POLICY["max_events"]):
        length = len(canonical(receipt).encode())
        if size + length <= POLICY["max_receipt_bytes"]:
            selected.append(receipt)
            size += length
    all_events = checked_events(selected, mint)
    own = [e for e in all_events if e.get("token_address") == mint]
    if not own:
        raise ValueError("evidence_pending")
    events = event_window(all_events, mint)
    view = project(events, mint)
    payload = {
        "schema": POLICY["schema"],
        "policy": POLICY,
        "source_digest": source_identity(),
        "coin_id": coin["id"],
        "token_address": mint,
        "symbol": str(coin.get("symbol") or "")[:20],
        "network": "solana",
        "receipts": selected,
        "events": events,
        "receipt_root": merkle_root(selected),
        "timing": TIMING,
        "coverage": {
            **coverage,
            "candidate_receipts": len(candidates),
            "saved_receipts": len(selected),
            "decoded_events": len(all_events),
            "saved_events": len(events),
            "drawn_events": view["drawn_events"],
            "drawn_nodes": view["drawn_nodes"],
            "keyframes": len(view["frames"]),
            "basis": "Collected transaction window; shared balances use one post-state.",
        },
        "launch": view["launch"],
        "frames": view["frames"],
        "quality": {"passed": True, "controls": POLICY["controls"]},
    }
    return {**payload, "id": digest(payload)}


def verify_replay(payload: dict) -> bool:
    """Verify saved receipts and reproduce the visual model before serving it."""
    try:
        body = {k: v for k, v in payload.items() if k != "id"}
        if (
            digest(body) != payload["id"]
            or payload["schema"] != POLICY["schema"]
            or payload["policy"] != POLICY
            or payload["network"] != "solana"
            or payload["timing"] != TIMING
            or payload["quality"] != {"passed": True, "controls": POLICY["controls"]}
        ):
            return False
        mint = _address(payload["token_address"])
        receipts = payload["receipts"]
        if (
            len(receipts) > POLICY["max_events"]
            or sum(len(canonical(r).encode()) for r in receipts) > POLICY["max_receipt_bytes"]
            or merkle_root(receipts) != payload["receipt_root"]
        ):
            return False
        decoded = checked_events(receipts, mint)
        events = payload["events"]
        if (
            not events
            or len(events) > POLICY["max_events"]
            or events != sorted(events, key=_order)
            or events != event_window(decoded, mint)
        ):
            return False
        view = project(events, mint)
        return (
            view["frames"] == payload["frames"]
            and view["launch"] == payload["launch"]
            and payload["coverage"]["saved_events"] == len(events)
            and payload["coverage"]["saved_receipts"] == len(receipts)
            and payload["coverage"]["decoded_events"] == len(decoded)
            and all(
                payload["coverage"][key] == view[key] for key in ("drawn_events", "drawn_nodes")
            )
        )
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        return False


def blend(first: dict, last: dict, progress: float, *, reset: bool = False) -> dict:
    t = min(1, max(0, progress))
    t = t * t * (3 - 2 * t)
    before = {n["id"]: n for n in first["nodes"]}
    after = {n["id"]: n for n in last["nodes"]}
    nodes = []
    for key in list(after) + ([k for k in before if k not in after] if reset else []):
        end = after.get(key)
        start = before.get(key)
        if start is None:
            parent = before.get(end.get("parent"), before["launch"])
            start = {**parent, "r": 0}
        if end is None:
            end = {**last["nodes"][0], "r": 0}
        node = {
            **after.get(key, before.get(key)),
            "opacity": 1 - t if key not in after else t if key not in before else 1,
        }
        node.update({name: start[name] + (end[name] - start[name]) * t for name in ("x", "y", "r")})
        nodes.append(node)
    return {**last, "nodes": nodes, "edges": first["edges"] if reset else last["edges"]}
