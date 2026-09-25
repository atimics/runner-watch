"""Build a daily market story from the edition's saved scanner evidence."""

from __future__ import annotations

from typing import Any

from runner_web.report_spotlight import number, select_spotlight, time_label, timely_quote

MAX_STORIES = 5


def select_stories(rows: list[dict[str, Any]], as_of: str) -> list[dict[str, Any]]:
    """Lead with research interest, then include a counterpoint and volume leader."""
    remaining = {
        str(row["ticker"]): row
        for row in rows
        if row.get("ticker") and timely_quote(row, as_of) and (number(row.get("price")) or 0) > 0
    }
    lead = select_spotlight(list(remaining.values()))
    if lead is None:
        return []
    selected = [remaining.pop(str(lead["ticker"]))]
    direction = number(lead.get("change_pct")) or 0
    opposite = [
        row for row in remaining.values() if (number(row.get("change_pct")) or 0) * direction < 0
    ]
    if opposite:
        counter = max(
            sorted(opposite, key=lambda row: str(row["ticker"])),
            key=lambda row: abs(number(row.get("change_pct")) or 0),
        )
        selected.append(remaining.pop(str(counter["ticker"])))
    if remaining:
        volume = max(
            sorted(remaining.values(), key=lambda row: str(row["ticker"])),
            key=lambda row: number(row.get("relative_volume")) or 0,
        )
        selected.append(remaining.pop(str(volume["ticker"])))
    while remaining and len(selected) < MAX_STORIES:
        chosen = select_spotlight(list(remaining.values()))
        selected.append(remaining.pop(str(chosen["ticker"])))
    return [dict(row) for row in selected]


def freeze_story_board(
    database: Any, rows: list[dict[str, Any]], as_of: str, captured_at: str
) -> list[dict[str, Any]]:
    selected = select_stories(rows, as_of)
    for row in selected:
        company = database.execute(
            "SELECT name FROM sec_companies WHERE ticker=? AND refreshed_at<=? "
            "ORDER BY refreshed_at DESC,cik LIMIT 1",
            (row["ticker"], captured_at),
        ).fetchone()
        row["company_name"] = company["name"] if company else row["ticker"]
    return selected


def _move(row: dict[str, Any]) -> str:
    move = number(row.get("change_pct"))
    if move is None:
        return f"{row['ticker']} draws attention"
    verb = "surges" if move >= 15 else "slides" if move <= -5 else "rises" if move > 0 else "eases"
    if move == 0:
        return f"{row['ticker']} holds steady"
    return f"{row['ticker']} {verb} {abs(move):.1f}%"


def _legacy_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    if report["report_type"] == "pre_market":
        return report.get("leaders") or []
    # Evening prose uses evening quotes. Opening rows retain their watch-board role.
    rows = []
    feature = report.get("spotlight") or {}
    if feature.get("snapshot"):
        rows.append({**feature["snapshot"], "company_name": feature.get("company", {}).get("name")})
    for row in report.get("leaders") or []:
        if row.get("ticker") == feature.get("ticker") or row.get("close_is_settled"):
            continue
        rows.append(
            {
                "ticker": row["ticker"],
                "price": row.get("close_price"),
                "quote_time": row.get("close_quote_time"),
                "change_pct": row.get("close_change_pct"),
                "relative_volume": row.get("close_relative_volume"),
                "trade_state": row.get("close_trade_state"),
            }
        )
    return rows


def build_narrative(report: dict[str, Any]) -> dict[str, Any]:
    post = report["report_type"] == "post_market"
    metrics = report.get("metrics") or {}
    saved = metrics.get("story_board")
    rows = select_stories(
        saved if isinstance(saved, list) else _legacy_rows(report), str(report.get("as_of") or "")
    )
    watch = {row["ticker"]: row for row in report.get("leaders") or []}
    stories = []
    for index, row in enumerate(rows):
        ticker = row["ticker"]
        move = number(row.get("change_pct"))
        volume = number(row.get("relative_volume"))
        blockbuster = abs(move or 0) >= 15 and (volume or 0) >= 3
        opposite = index > 0 and (move or 0) * (number(rows[0].get("change_pct")) or 0) < 0
        label = (
            "Blockbuster move"
            if blockbuster and index == 0
            else "The main story"
            if index == 0
            else "The counterpoint"
            if opposite
            else "Volume watch"
            if (volume or 0) >= 3
            else "Also in play"
        )
        name = row.get("company_name") or ticker
        subject = f"{name} ({ticker})" if name != ticker else ticker
        action = f"was {'up' if move > 0 else 'down'} {abs(move):.1f}%" if move else "held steady"
        if move is None:
            action = "appeared in the saved scan"
        body = [f"{subject} {action} at {time_label(row.get('quote_time'))}."]
        if volume is not None:
            body.append(f"Trading volume ran at {volume:.1f} times its usual level.")
        signals = row.get("signals") or []
        if signals:
            body.append(f"The scan highlighted {str(signals[0]).rstrip('.')}.")
        board_row = watch.get(ticker) or {}
        if post:
            change = number(board_row.get("session_return_pct"))
            if change is not None:
                reference = (
                    "settled close" if board_row.get("close_is_settled") else "evening quote"
                )
                body.append(f"Its {reference} stood {change:+.1f}% from the opening watch price.")
            elif ticker not in watch:
                body.append("It joined the story after the opening watch was saved.")
        forecast = board_row.get("eod_forecast") or {}
        target = number(forecast.get("target_price"))
        if target is not None:
            if post and forecast.get("status") in {"hit", "miss"}:
                result = "hit" if forecast["status"] == "hit" else "missed"
                body.append(f"Flash's ${target:.4f} closing target was {result}.")
            elif not post:
                body.append(f"Flash's saved target puts ${target:.4f} in view for the close.")
        risks = row.get("risks") or []
        if risks:
            body.append(f"The risk to carry forward: {str(risks[0]).rstrip('.')}.")
        if row.get("trade_state") in {"AVOID", "EXIT"}:
            body.append(f"The saved trade state was {row['trade_state']}.")
        stories.append(
            {"ticker": ticker, "label": label, "headline": _move(row), "body": " ".join(body)}
        )

    headline = (
        stories[0]["headline"] if stories else str(report.get("headline") or "The session in view")
    )
    if len(stories) > 1:
        headline += f" as {stories[1]['headline']}"
    breadth = metrics.get("closing_breadth") if post else metrics
    intro = []
    if isinstance(breadth, dict) and breadth.get("candidates"):
        intro.append(
            f"{'The evening' if post else 'The early'} scan found {breadth['candidates']} "
            f"{'name' if breadth['candidates'] == 1 else 'names'}: "
            f"{breadth.get('green', 0)} rising and {breadth.get('red', 0)} falling."
        )
    if stories:
        names = ", ".join(story["ticker"] for story in stories)
        lead_in = (
            "The session comes into focus through"
            if post
            else "The opening watch takes shape around"
        )
        intro.append(f"{lead_in} {names}.")
    if post and metrics.get("joined"):
        intro.append(f"{metrics['joined']} names joined the scan after the opening watch.")
    next_read = (
        "Carry these moves into the next session. Watch whether volume returns and how prices "
        "compare with the saved evening quotes."
        if post
        else "The opening bell brings the next test: whether these moves hold as volume builds. "
        "Compare the next quotes with the watch prices and Flash's saved closing targets below."
    )
    return {
        "headline": headline,
        "intro": " ".join(intro) or str(report.get("summary") or ""),
        "stories": stories,
        "next_heading": "The next chapter" if post else "The test at the open",
        "next_read": next_read,
    }
