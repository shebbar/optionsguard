"""FastMCP server, stdio transport. Per DESIGN.md section 14: each tool is a
thin wrapper over the core's /api/v1 JSON API. This module never imports the
core or touches the DB -- if the core isn't running, every tool returns a
clear error telling the user to start it.

All numbers here are computed by the core service, never estimated. Tool
descriptions tell the calling model to report the returned numbers and
flags verbatim rather than guess at them.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

OPTIONGUARD_URL = os.environ.get("OPTIONGUARD_URL", "http://127.0.0.1:8787")
START_HINT = "Start OptionGuard: uv run optionguard serve"

mcp = MCPServer(
    "optionguard",
    instructions=(
        "OptionGuard evaluates NSE stock-option trades before entry and enforces personal "
        "risk rules. Every number (probabilities, greeks, max lots, verdicts) is computed "
        "by the core service -- always report the fields returned by these tools verbatim, "
        "never estimate or recompute them yourself. Probabilities are risk-neutral, implied "
        "by market prices -- not forecasts of direction. If a tool returns an `error`, surface "
        "its `message` to the user verbatim; do not silently retry or work around a guardrail."
    ),
)


def _request(method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict[str, Any]:
    try:
        with httpx.Client(base_url=OPTIONGUARD_URL, timeout=30.0) as client:
            resp = client.request(method, path, json=json, params=params)
    except httpx.ConnectError:
        return {"error": {"code": "CORE_NOT_RUNNING", "message": START_HINT}}
    try:
        body = resp.json()
    except ValueError:
        body = {"error": {"code": "BAD_RESPONSE", "message": resp.text[:500]}}
    if resp.status_code >= 400 and "error" not in body:
        body = {"error": {"code": "HTTP_ERROR", "message": f"{resp.status_code}: {body}"}}
    return body


@mcp.tool()
def check_options(
    candidates: list[str],
    thesis: list[str] | None = None,
    source: str = "kite",
    manual_inputs: list[dict] | None = None,
) -> dict:
    """Evaluate one or more candidate option trades before entry.

    candidates: text like "TCS NOV 2500 CE", one per candidate.
    thesis: optional parallel list, e.g. "@2550 by 2026-11-10" (same length as candidates, use "" to skip one).
    source: "kite" (default -- live quotes, or a mock standing in for Kite until real API keys exist) or "manual".
    manual_inputs: required when source="manual", one dict per candidate with keys
        spot, premium, lot_size, expiry (YYYY-MM-DD), and optionally bid, ask, hv20, volume, oi.

    Returns the full metrics, verdict (red/amber/green), flags, max position size,
    alternatives, OI context, holding-period scenarios and explanation_facts for each
    candidate, computed by the core -- report them verbatim rather than estimating.

    Each result also has delta, daily_move_needed (how much the underlying must move your
    way per trading day just to offset theta, in both price and %), and scenarios: a grid
    of estimated P&L per lot for holding 1/3/5 trading days through a -2%..+2% underlying
    move, IV held constant. Most trades are exited within days, not held to expiry --
    report the scenario grid rather than only the expiry-day probabilities when discussing
    whether a trade is worth taking.

    Each result's data_source will be "mock" rather than "kite" if no real Kite credentials
    are configured -- always check this and tell the user clearly when numbers are simulated,
    not real. explanation_facts also leads with a MOCK DATA warning in that case.
    """
    items = []
    for i, text in enumerate(candidates):
        entry: dict[str, Any] = {"text": text}
        t = thesis[i] if thesis and i < len(thesis) else None
        if t:
            entry["thesis"] = t
        if manual_inputs and i < len(manual_inputs):
            entry["manual"] = manual_inputs[i]
        items.append(entry)
    return _request("POST", "/api/v1/checks", json={"candidates": items, "source": source})


@mcp.tool()
def get_positions() -> dict:
    """List open positions with live premium, unrealized P&L, and distance to each stop.
    Each position's data_source ("manual"/"mock"/"kite") tells you whether its numbers are
    real -- "mock" means simulated, report it as such. has_gtt is True/False for kite/mock
    positions (whether a matching GTT order exists in Kite to actually enforce the stop) or
    null for manual positions where GTT doesn't apply; a False has_gtt means this stop exists
    only as a number in this app, not as a real protective order."""
    return {"positions": _request("GET", "/api/v1/positions")}


@mcp.tool()
def get_status() -> dict:
    """Kite login/mock state, month realised P&L, loss-limit headroom, and cooling-off state."""
    return _request("GET", "/api/v1/status")


@mcp.tool()
def record_trade(
    check_id: int,
    lots: int,
    entry_premium: float,
    stop_premium: float | None = None,
    thesis_stop_level: float | None = None,
    time_stop_date: str | None = None,
    override_reason: str | None = None,
) -> dict:
    """Record a trade against a check you already ran with check_options. The check must be
    less than 30 minutes old and lots must not exceed the check's max_lots -- both hard,
    non-overridable limits. A red-verdict check needs override_reason (15+ characters).
    If this returns an `error`, surface its message verbatim -- it is a guardrail, not a bug."""
    body = {"check_id": check_id, "lots": lots, "entry_premium": entry_premium}
    if stop_premium is not None:
        body["stop_premium"] = stop_premium
    if thesis_stop_level is not None:
        body["thesis_stop_level"] = thesis_stop_level
    if time_stop_date is not None:
        body["time_stop_date"] = time_stop_date
    if override_reason is not None:
        body["override_reason"] = override_reason
    return _request("POST", "/api/v1/trades", json=body)


@mcp.tool()
def tighten_stop(
    trade_id: int,
    stop_premium: float | None = None,
    thesis_stop_level: float | None = None,
    time_stop_date: str | None = None,
) -> dict:
    """Make a stop more protective: a higher stop_premium, a thesis_stop_level closer to
    spot, or an earlier time_stop_date (YYYY-MM-DD). Loosening a stop is not available here --
    it requires a reason and must be done in the web UI."""
    body = {}
    if stop_premium is not None:
        body["stop_premium"] = stop_premium
    if thesis_stop_level is not None:
        body["thesis_stop_level"] = thesis_stop_level
    if time_stop_date is not None:
        body["time_stop_date"] = time_stop_date
    return _request("POST", f"/api/v1/trades/{trade_id}/tighten", json=body)


@mcp.tool()
def exit_trade(trade_id: int, exit_premium: float, exit_reason: str, charges: float | None = None) -> dict:
    """Close an open trade. exit_reason is one of: premium_stop, thesis_stop, time_stop,
    target, manual, expiry."""
    body = {"exit_premium": exit_premium, "exit_reason": exit_reason}
    if charges is not None:
        body["charges"] = charges
    return _request("POST", f"/api/v1/trades/{trade_id}/exit", json=body)


@mcp.tool()
def journal_stats(
    from_date: str | None = None, to_date: str | None = None, group_by: str = "verdict"
) -> dict:
    """Trade stats grouped by verdict, exit_reason, month, symbol, or override. Dates are
    YYYY-MM-DD."""
    params = {"group_by": group_by}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    return {"stats": _request("GET", "/api/v1/journal/stats", params=params)}


@mcp.tool()
def list_checks(
    from_date: str | None = None, to_date: str | None = None, verdict: str | None = None, traded: bool | None = None
) -> dict:
    """List past checks (traded and skipped), with expiry_settle once backfilled, so outcomes
    can be compared even for candidates that were never traded."""
    params: dict[str, Any] = {}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if verdict:
        params["verdict"] = verdict
    if traded is not None:
        params["traded"] = traded
    return {"checks": _request("GET", "/api/v1/journal/checks", params=params)}


@mcp.tool()
def add_corporate_event(symbol: str, event_type: str, event_date: str, dividend: float | None = None) -> dict:
    """Record an earnings date or dividend (event_type: "earnings" or "dividend") so future
    checks can flag EARNINGS_INSIDE and adjust pricing for the escrowed dividend."""
    body: dict[str, Any] = {"symbol": symbol, "event_type": event_type, "event_date": event_date}
    if dividend is not None:
        body["dividend"] = dividend
    return _request("POST", "/api/v1/events", json=body)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
