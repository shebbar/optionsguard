from __future__ import annotations

from optionguard.api import status as status_api
from optionguard.market import factory


def test_manual_check_trade_tighten_exit_flow(client):
    resp = client.post(
        "/api/v1/checks",
        json={
            "source": "manual",
            "candidates": [{
                "text": "TCS NOV 2500 CE",
                "manual": {"spot": 2480, "premium": 35, "lot_size": 175, "expiry": "2026-11-26"},
            }],
        },
    )
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["verdict"] in ("red", "amber", "green")
    check_id = result["id"]

    trade_resp = client.post(
        "/api/v1/trades",
        json={"check_id": check_id, "lots": 1, "entry_premium": 35, "stop_premium": 20, "time_stop_date": "2026-11-17"},
    )
    if result["max_lots"] >= 1 and result["verdict"] != "red":
        assert trade_resp.status_code == 200
        trade_id = trade_resp.json()["id"]

        tighten_resp = client.post(f"/api/v1/trades/{trade_id}/tighten", json={"stop_premium": 25})
        assert tighten_resp.status_code == 200
        assert tighten_resp.json()["stop_premium"] == 25

        loosen_attempt = client.post(f"/api/v1/trades/{trade_id}/tighten", json={"stop_premium": 10})
        assert loosen_attempt.status_code == 409

        exit_resp = client.post(f"/api/v1/trades/{trade_id}/exit", json={"exit_premium": 45, "exit_reason": "target"})
        assert exit_resp.status_code == 200
        assert exit_resp.json()["status"] == "closed"
    else:
        assert trade_resp.status_code == 409


def test_checks_over_max_lots_rejected(client):
    check_resp = client.post(
        "/api/v1/checks",
        json={
            "source": "manual",
            "candidates": [{
                "text": "TCS NOV 2500 CE",
                "manual": {"spot": 2480, "premium": 35, "lot_size": 175, "expiry": "2026-11-26"},
            }],
        },
    )
    check_id = check_resp.json()["results"][0]["id"]
    resp = client.post("/api/v1/trades", json={"check_id": check_id, "lots": 10_000, "entry_premium": 35})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "OVER_MAX_LOTS"


def test_mock_backed_kite_check_is_tagged_mock_not_kite(client, monkeypatch):
    """The core bug this guards against: a check run with source="kite" while
    no real credentials exist must be stored and returned as data_source
    "mock", never "kite" -- otherwise simulated and real data become
    indistinguishable in the journal once real credentials are added later."""
    monkeypatch.setattr(factory, "is_kite_mock", lambda: True)
    monkeypatch.setattr(status_api, "is_kite_mock", lambda: True)

    # The mock's TCS price is a years-long random walk from a fixed 2024
    # epoch, so a hardcoded strike will eventually drift out of the current
    # strike grid -- look up a live-relative one instead, same as a real
    # caller would via a chain/quote before picking a strike.
    mock_provider = factory.get_mock_kite_provider()
    live_strike = round(mock_provider.spot("TCS") / 50) * 50
    candidate_text = f"TCS NOV {live_strike} CE"

    resp = client.post("/api/v1/checks", json={"source": "kite", "candidates": [{"text": candidate_text}]})
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["data_source"] == "mock"
    assert any("MOCK DATA" in f for f in result["explanation_facts"])

    fetched = client.get(f"/api/v1/checks/{result['id']}").json()
    assert fetched["data_source"] == "mock"

    status = client.get("/api/v1/status").json()
    assert status["kite_mode"] == "mock"


def test_real_kite_check_without_session_returns_login_required(client, monkeypatch):
    monkeypatch.setattr(factory, "is_kite_mock", lambda: False)

    resp = client.post("/api/v1/checks", json={"source": "kite", "candidates": [{"text": "TCS NOV 2500 CE"}]})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "KITE_LOGIN_REQUIRED"
    assert "login_url" in resp.json()["error"]


def test_positions_surfaces_has_gtt_for_mock_none_for_manual(client, db, monkeypatch):
    monkeypatch.setattr(factory, "is_kite_mock", lambda: True)

    # Generous settings so sizing never gets in the way of this test's point
    # (has_gtt semantics), independent of how expensive the mock's premium happens to be.
    from datetime import datetime, timezone

    from optionguard.db import repo

    repo.insert_settings(
        db, effective_from=datetime.now(timezone.utc), max_loss_per_trade=10_000_000,
        monthly_loss_limit=10_000_000, premium_stop_pct=0.4, time_stop_days=7, cooloff_days=10, show_spreads=False,
    )

    mock_provider = factory.get_mock_kite_provider()
    live_strike = round(mock_provider.spot("TCS") / 50) * 50

    check_resp = client.post(
        "/api/v1/checks", json={"source": "kite", "candidates": [{"text": f"TCS NOV {live_strike} CE"}]}
    )
    check = check_resp.json()["results"][0]
    trade_resp = client.post(
        "/api/v1/trades",
        json={
            "check_id": check["id"], "lots": 1, "entry_premium": check["premium"], "stop_premium": 0.01,
            # The mock's price path is date-dependent, so TCS's verdict on
            # any given day could be red -- this test is about has_gtt
            # semantics, not verdict, so always supply a valid override.
            "override_reason": "test fixture: verdict is incidental to this test",
        },
    )
    assert trade_resp.status_code == 200, trade_resp.json()

    positions = client.get("/api/v1/positions").json()
    mock_position = next(p for p in positions if p["id"] == trade_resp.json()["id"])
    assert mock_position["data_source"] == "mock"
    assert mock_position["has_gtt"] is False  # mock never has real GTT orders

    # premium=35 (not something tiny like 1) keeps IV realistic for this
    # spot/strike -- an implausibly cheap premium drives IV near zero, which
    # makes the breakeven look many sigmas away and forces a red verdict
    # regardless of what this test is actually checking (has_gtt semantics).
    manual_check_resp = client.post(
        "/api/v1/checks",
        json={
            "source": "manual",
            "candidates": [{"text": "TCS NOV 2500 CE", "manual": {"spot": 2480, "premium": 35, "lot_size": 175, "expiry": "2026-11-26"}}],
        },
    )
    manual_check = manual_check_resp.json()["results"][0]
    manual_trade_resp = client.post(
        "/api/v1/trades",
        json={
            "check_id": manual_check["id"], "lots": 1, "entry_premium": 35, "stop_premium": 0.01,
            # Same reasoning as the mock trade above -- e.g. the hardcoded
            # expiry date here drifts closer to "today" as time passes,
            # which can trip NEAR_EXPIRY on its own regardless of premium.
            "override_reason": "test fixture: verdict is incidental to this test",
        },
    )
    assert manual_trade_resp.status_code == 200, manual_trade_resp.json()
    manual_trade = manual_trade_resp.json()

    positions = client.get("/api/v1/positions").json()
    manual_position = next(p for p in positions if p["id"] == manual_trade["id"])
    assert manual_position["has_gtt"] is None  # not applicable -- manual mode has no GTT concept


def test_ui_settings_rejects_without_csrf(client):
    resp = client.post("/settings-html", data={"max_loss_per_trade": "9999"})
    assert resp.status_code == 403


def test_ui_settings_accepts_with_csrf(client):
    page = client.get("/settings")
    assert page.status_code == 200
    assert "og_session" in client.cookies

    resp = client.post("/settings-html", data={"max_loss_per_trade": "9999"})
    assert resp.status_code == 403  # cookie alone isn't enough; needs the token too

    settings_page_text = page.text
    import re

    m = re.search(r'name="csrf_token" value="([^"]+)"', settings_page_text)
    assert m, "csrf token not found on settings page"
    resp2 = client.post("/settings-html", data={"max_loss_per_trade": "9999", "csrf_token": m.group(1)})
    assert resp2.status_code == 200
