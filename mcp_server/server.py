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
    only as a number in this app, not as a real protective order. gtt_status (section 7.8,
    added) is "active"/"missing"/"triggered"/"deleted"/null for a position placed through
    the order ticket -- "missing" or "triggered" both mean the position is currently
    unprotected and the user should check the web UI's Positions screen."""
    return {"positions": _request("GET", "/api/v1/positions")}


@mcp.tool()
def list_closed_trades() -> dict:
    """List closed (exited) trades with entry/exit premium, exit reason and realized P&L.
    Each trade's data_source ("manual"/"mock"/"kite") tells you whether its numbers are
    real -- "mock" means simulated, report it as such."""
    return {"trades": _request("GET", "/api/v1/trades/closed")}


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
    from_date: str | None = None, to_date: str | None = None, verdict: str | None = None, traded: bool | None = None,
    acted: bool | None = None,
) -> dict:
    """List past checks (traded and skipped), with expiry_settle once backfilled, so outcomes
    can be compared even for candidates that were never traded. acted=True limits this to
    checks that led to a trade or an option thesis (the same "acted on" filter the web UI's
    Journal screen defaults to) -- use this instead of traded when the user wants their
    serious candidates, not every exploratory check."""
    params: dict[str, Any] = {}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if verdict:
        params["verdict"] = verdict
    if traded is not None:
        params["traded"] = traded
    if acted is not None:
        params["acted"] = acted
    return {"checks": _request("GET", "/api/v1/journal/checks", params=params)}


@mcp.tool()
def add_corporate_event(symbol: str, event_type: str, event_date: str, dividend: float | None = None) -> dict:
    """Record an earnings date or dividend (event_type: "earnings" or "dividend") so future
    checks can flag EARNINGS_INSIDE and adjust pricing for the escrowed dividend."""
    body: dict[str, Any] = {"symbol": symbol, "event_type": event_type, "event_date": event_date}
    if dividend is not None:
        body["dividend"] = dividend
    return _request("POST", "/api/v1/events", json=body)


@mcp.tool()
def get_settings() -> dict:
    """Current risk settings: max_loss_per_trade, monthly_loss_limit, premium_stop_pct
    (the floor for how loose a stop can be), time_stop_days, cooloff_days, show_spreads,
    and effective_from. Read-only -- there is no tool to change these, only the web UI's
    Settings screen can (and loosening a limit is delayed to the next trading day there).
    Use this alongside get_status (realised P&L, headroom) when reasoning about whether a
    new trade fits within the user's own risk limits."""
    return _request("GET", "/api/v1/settings")


@mcp.tool()
def list_sectors() -> dict:
    """Configured sectors (name -> benchmark index), for building add_option_thesis's
    sector argument."""
    return _request("GET", "/api/v1/sectors")


@mcp.tool()
def sync_status() -> dict:
    """The most recent data sync's time and result (orders/GTTs reconciled, checks
    settled, option theses bars/resolve/score/policies, and any errors). There is no sync
    trigger over MCP -- syncing happens automatically on Kite login or from the web UI's
    "Resolve & score" button, never from here."""
    return _request("GET", "/api/v1/sync/latest")


@mcp.tool()
def list_order_intents(status: str | None = None) -> dict:
    """Section 7.8, added, read-only. Lists order_intents (reviewed/placed/filled/partial/
    rejected/cancelled/expired) -- the record of entry/exit tickets reviewed and confirmed
    in the web UI. status filters to one of those values if given. There is no tool to
    place, modify or cancel an order here, or to review/confirm one -- orders can only be
    placed from the web UI's order ticket, never from Claude."""
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    return {"intents": _request("GET", "/api/v1/orders/intents", params=params)}


@mcp.tool()
def preview_option_thesis(
    contract: str, target_premium: float, stop_premium: float, duration_days: int, lots: int = 1, source: str = "kite",
) -> dict:
    """Section 7.9: live derived numbers for a planned option thesis -- a view on the
    OPTION'S OWN PREMIUM (entry/target/stop), not the underlying's price -- before saving
    anything. Ask the user for target_premium, stop_premium and duration_days yourself;
    never choose or fill these in on their own judgement. Report reward_risk, risk_per_lot,
    spot_for_target/spot_for_stop (the underlying level needed for each), and the market-
    implied odds (mkt_prob_target_first/stop_first/time_exit, from Monte Carlo under
    today's IV -- risk-neutral odds, not a forecast) verbatim."""
    return _request(
        "POST", "/api/v1/option-theses/preview",
        json={
            "contract": contract, "source": source, "target_premium": target_premium,
            "stop_premium": stop_premium, "duration_days": duration_days, "lots": lots,
        },
    )


@mcp.tool()
def add_option_thesis(
    check_id: int,
    why: str,
    target_premium: float,
    stop_premium: float,
    duration_days: int,
    lots: int = 1,
    sector: str | None = None,
    override_reason: str | None = None,
    notes: str | None = None,
) -> dict:
    """Record a PAPER option thesis (section 7.9) against a check you already ran with
    check_options -- paper only; mode="real" is not available here, it needs the web UI's
    "Save & buy" flow. The check must be less than 30 minutes old.

    Ask the user for why (10-200 chars, the one-line reason), target_premium, stop_premium
    and duration_days themselves -- never choose these on their own judgement. Call
    preview_option_thesis first and report its market-implied odds so the user can decide
    with that in hand. A red-verdict check needs override_reason (15+ characters) -- ask
    the user for one rather than inventing it. If this returns an `error`, surface its
    message verbatim -- it is a guardrail, not a bug."""
    body: dict[str, Any] = {
        "check_id": check_id, "mode": "paper", "why": why, "target_premium": target_premium,
        "stop_premium": stop_premium, "duration_days": duration_days, "lots": lots,
    }
    if sector is not None:
        body["sector"] = sector
    if override_reason is not None:
        body["override_reason"] = override_reason
    if notes is not None:
        body["notes"] = notes
    return _request("POST", "/api/v1/option-theses", json=body)


@mcp.tool()
def list_option_theses(
    status: str | None = None, mode: str | None = None, sector: str | None = None, symbol: str | None = None,
    verdict: str | None = None, from_date: str | None = None, to_date: str | None = None,
) -> dict:
    """List option theses (section 7.9). status: "open"|"closed"|"scored"|"cancelled".
    mode: "paper"|"real"."""
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    if mode:
        params["mode"] = mode
    if sector:
        params["sector"] = sector
    if symbol:
        params["symbol"] = symbol
    if verdict:
        params["verdict"] = verdict
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date
    return {"option_theses": _request("GET", "/api/v1/option-theses", params=params)}


@mcp.tool()
def get_option_thesis(thesis_id: int) -> dict:
    """One option thesis (section 7.9) with its score and policy comparison results (once
    scored) or interim progress (live premium vs target/stop, days left) while still open."""
    return _request("GET", f"/api/v1/option-theses/{thesis_id}")


@mcp.tool()
def update_option_thesis_notes(thesis_id: int, notes: str) -> dict:
    """Replace an option thesis's freeform notes field (section 7.9). Does not touch
    target/stop/duration or anything guardrail-relevant -- just the notes text."""
    return _request("PATCH", f"/api/v1/option-theses/{thesis_id}/notes", json={"notes": notes})


@mcp.tool()
def option_thesis_scorecard(
    from_date: str | None = None, to_date: str | None = None, mode: str = "all", group_by: str = "none",
) -> dict:
    """Aggregated stats across scored option theses (section 7.9) -- mock and cancelled
    theses excluded, excluded counts reported separately. mode: "paper"|"real"|"all".
    group_by: "sector"|"verdict"|"outcome"|"symbol"|"opt_type"|"override"|"month"|"none".
    Includes a policy comparison block (plain/grace3/time_dN/hold, each vs the thesis's own
    outcome). Report the findings sentences and rates verbatim -- they're computed from
    real price history, not estimated or inferred by you."""
    params: dict[str, Any] = {"mode": mode, "group_by": group_by}
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date
    return _request("GET", "/api/v1/option-theses/scorecard", params=params)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
