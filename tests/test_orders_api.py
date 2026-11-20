"""Section 7.8.1/7.8.2/7.8.9: isolation (MCP/API can never place or cancel
an order or a GTT) and the static-IP error detection that drives GET
/status's orders.ip_ok."""
from __future__ import annotations

import asyncio

from optionguard.db import repo
from optionguard.orders.service import is_ip_error_message


def test_mcp_has_no_order_action_tools():
    import sys

    sys.path.insert(0, ".")
    from mcp_server.server import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    forbidden = {"place_order", "cancel_order", "modify_order", "place_gtt", "modify_gtt", "delete_gtt",
                 "confirm_order", "review_order", "confirm_entry", "confirm_exit"}
    assert not (names & forbidden)
    assert "list_order_intents" in names  # the one read-only addition


def test_api_v1_has_no_order_placement_route(client):
    """Only /ui/* routes (CSRF-protected) can place/modify/cancel an order
    or a GTT -- confirm none of them are reachable under /api/v1, which is
    what MCP talks to."""
    for path in ("/api/v1/order/review", "/api/v1/order/confirm", "/api/v1/order/1/modify", "/api/v1/order/1/cancel"):
        resp = client.post(path, json={})
        assert resp.status_code == 404, f"{path} should not exist under /api/v1 (got {resp.status_code})"

    # The one /api/v1 route that does exist for orders is read-only.
    resp = client.get("/api/v1/orders/intents")
    assert resp.status_code == 200


def test_ui_order_routes_require_csrf(client):
    for path in ("/ui/order/review", "/ui/order/confirm"):
        resp = client.post(path, json={"kind": "entry"} if "review" in path else {"intent_id": 1})
        assert resp.status_code == 403


# ---- IP error detection (7.8.2) --------------------------------------------

def test_is_ip_error_message_matches_ip_whitelist_wording():
    assert is_ip_error_message("Invalid or unauthorized IP, not whitelisted.")
    assert is_ip_error_message("Static IP not valid for this app.")
    assert not is_ip_error_message("Insufficient margin for this order.")
    assert not is_ip_error_message(None)


def test_status_reports_null_ip_ok_before_any_order_attempt(client, db):
    resp = client.get("/api/v1/status")
    assert resp.json()["orders"]["ip_ok"] is None


def test_status_reports_ip_false_after_an_ip_rejection(client, db):
    from optionguard.db.models import Check
    from datetime import datetime, timezone

    check = Check(
        created_at=datetime.now(timezone.utc), batch_id="t", symbol="TCS", expiry="2026-12-31",
        strike=7000, opt_type="CE", data_source="mock", spot=100, premium=10, bid=9, ask=11, lot_size=175,
        iv=None, hv20=None, hv_percentile=None, breakeven=None, breakeven_pct=None, expected_move_pct=None,
        prob_profit=None, prob_zero=None, prob_thesis=None, theta_per_day_lot=None, delta=None,
        daily_move_needed=None, stretch_5d_pct=None, stretch_20dma_pct=None, spread_pct=None,
        thesis_target=None, thesis_date=None, verdict="green", flags=[], max_lots=10, alternatives=None,
        oi_context=None, raw={},
    )
    db.add(check)
    db.commit()
    db.refresh(check)

    repo.create_order_intent(
        db, kind="entry", check_id=check.id, tradingsymbol="TCS|2026-12-31|7000|CE",
        quantity=175, order_type="LIMIT", price=100,
    )
    intent = repo.list_order_intents(db)[0]
    repo.try_transition_order_intent(db, intent.id, from_status="reviewed", to_status="placed")
    repo.update_order_intent(
        db, intent.id, status="rejected", kite_message="Invalid or unauthorized IP, not whitelisted.",
    )

    resp = client.get("/api/v1/status")
    body = resp.json()["orders"]
    assert body["ip_ok"] is False
    assert "whitelisted" in body["last_ip_error"]


def test_status_reports_ip_true_after_a_non_ip_rejection(client, db):
    from optionguard.db.models import Check
    from datetime import datetime, timezone

    check = Check(
        created_at=datetime.now(timezone.utc), batch_id="t", symbol="TCS", expiry="2026-12-31",
        strike=7000, opt_type="CE", data_source="mock", spot=100, premium=10, bid=9, ask=11, lot_size=175,
        iv=None, hv20=None, hv_percentile=None, breakeven=None, breakeven_pct=None, expected_move_pct=None,
        prob_profit=None, prob_zero=None, prob_thesis=None, theta_per_day_lot=None, delta=None,
        daily_move_needed=None, stretch_5d_pct=None, stretch_20dma_pct=None, spread_pct=None,
        thesis_target=None, thesis_date=None, verdict="green", flags=[], max_lots=10, alternatives=None,
        oi_context=None, raw={},
    )
    db.add(check)
    db.commit()
    db.refresh(check)

    repo.create_order_intent(
        db, kind="entry", check_id=check.id, tradingsymbol="TCS|2026-12-31|7000|CE",
        quantity=175, order_type="LIMIT", price=100,
    )
    intent = repo.list_order_intents(db)[0]
    repo.try_transition_order_intent(db, intent.id, from_status="reviewed", to_status="placed")
    repo.update_order_intent(db, intent.id, status="rejected", kite_message="Insufficient margin.")

    resp = client.get("/api/v1/status")
    body = resp.json()["orders"]
    assert body["ip_ok"] is True
    assert body["last_ip_error"] is None
