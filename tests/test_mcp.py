"""Each MCP tool maps to the right core route, and a core-down error reads
clearly. Per DESIGN.md section 16. No live network calls for the mapping
checks -- _request is stubbed; only the core-down test hits a real
(unreachable) socket."""
from __future__ import annotations

import pytest

import mcp_server.server as srv


@pytest.fixture
def calls(monkeypatch):
    log = []

    def fake_request(method, path, *, json=None, params=None):
        log.append((method, path, json, params))
        return {"ok": True}

    monkeypatch.setattr(srv, "_request", fake_request)
    return log


def test_check_options_maps_to_post_checks(calls):
    srv.check_options(["TCS NOV 2500 CE"], source="manual", manual_inputs=[{"spot": 2480, "premium": 35, "lot_size": 175, "expiry": "2026-11-26"}])
    method, path, json, _ = calls[0]
    assert (method, path) == ("POST", "/api/v1/checks")
    assert json["source"] == "manual"
    assert json["candidates"][0]["manual"]["premium"] == 35


def test_get_positions_maps_to_get_positions(calls):
    srv.get_positions()
    assert calls[0][:2] == ("GET", "/api/v1/positions")


def test_get_status_maps_to_get_status(calls):
    srv.get_status()
    assert calls[0][:2] == ("GET", "/api/v1/status")


def test_record_trade_maps_to_post_trades(calls):
    srv.record_trade(check_id=7, lots=2, entry_premium=30, override_reason="a long enough override reason")
    method, path, json, _ = calls[0]
    assert (method, path) == ("POST", "/api/v1/trades")
    assert json["check_id"] == 7 and json["override_reason"]


def test_tighten_stop_maps_to_post_tighten(calls):
    srv.tighten_stop(trade_id=9, stop_premium=25)
    method, path, json, _ = calls[0]
    assert (method, path) == ("POST", "/api/v1/trades/9/tighten")
    assert json == {"stop_premium": 25}


def test_exit_trade_maps_to_post_exit(calls):
    srv.exit_trade(trade_id=9, exit_premium=45, exit_reason="target")
    method, path, json, _ = calls[0]
    assert (method, path) == ("POST", "/api/v1/trades/9/exit")
    assert json["exit_reason"] == "target"


def test_journal_stats_maps_to_get_journal_stats(calls):
    srv.journal_stats(group_by="symbol")
    method, path, _, params = calls[0]
    assert (method, path) == ("GET", "/api/v1/journal/stats")
    assert params["group_by"] == "symbol"


def test_list_checks_maps_to_get_journal_checks(calls):
    srv.list_checks(verdict="red")
    method, path, _, params = calls[0]
    assert (method, path) == ("GET", "/api/v1/journal/checks")
    assert params["verdict"] == "red"


def test_add_corporate_event_maps_to_post_events(calls):
    srv.add_corporate_event("TCS", "earnings", "2026-11-15")
    method, path, json, _ = calls[0]
    assert (method, path) == ("POST", "/api/v1/events")
    assert json["symbol"] == "TCS"


def test_preview_option_thesis_maps_to_post_preview(calls):
    srv.preview_option_thesis("TCS NOV 2500 CE", target_premium=60, stop_premium=30, duration_days=5)
    method, path, json, _ = calls[0]
    assert (method, path) == ("POST", "/api/v1/option-theses/preview")
    assert json["target_premium"] == 60 and json["duration_days"] == 5


def test_add_option_thesis_maps_to_post_option_theses_paper_only(calls):
    srv.add_option_thesis(
        check_id=7, why="testing the mcp tool mapping", target_premium=60, stop_premium=30, duration_days=5,
        override_reason="a long enough override reason",
    )
    method, path, json, _ = calls[0]
    assert (method, path) == ("POST", "/api/v1/option-theses")
    assert json["mode"] == "paper"
    assert json["check_id"] == 7 and json["override_reason"]


def test_list_option_theses_maps_to_get_option_theses(calls):
    srv.list_option_theses(status="open", mode="paper")
    method, path, _, params = calls[0]
    assert (method, path) == ("GET", "/api/v1/option-theses")
    assert params["status"] == "open" and params["mode"] == "paper"


def test_get_option_thesis_maps_to_get_option_thesis_by_id(calls):
    srv.get_option_thesis(9)
    assert calls[0][:2] == ("GET", "/api/v1/option-theses/9")


def test_option_thesis_scorecard_maps_to_get_scorecard(calls):
    srv.option_thesis_scorecard(group_by="sector", mode="real")
    method, path, _, params = calls[0]
    assert (method, path) == ("GET", "/api/v1/option-theses/scorecard")
    assert params["group_by"] == "sector" and params["mode"] == "real"


def test_core_down_error_is_readable():
    # Point at an unreachable port to exercise the error path deterministically.
    old_url = srv.OPTIONGUARD_URL
    srv.OPTIONGUARD_URL = "http://127.0.0.1:1"
    try:
        result = srv._request("GET", "/api/v1/status")
    finally:
        srv.OPTIONGUARD_URL = old_url
    assert "error" in result
    assert "uv run optionguard serve" in result["error"]["message"]
