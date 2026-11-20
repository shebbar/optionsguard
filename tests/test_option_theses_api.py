"""Section 7.9.3/7.9.4/7.9.10: the option theses API -- preview, create
(validation, duplicate-contract, red-verdict override, duration bounds),
list/get/notes, and the UI-only exit/cancel/link-trade routes."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from optionguard.config import get_config
from optionguard.db import repo
from optionguard.market import factory


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(factory, "is_kite_mock", lambda: True)
    factory.get_kite_client.cache_clear()
    return factory.get_mock_kite_provider()


def _expiry_month(nth: int = 2) -> str:
    from optionguard.market.mock_kite_client import monthly_expiries

    expiry = monthly_expiries(date.today(), count=nth)[nth - 1]
    return expiry.strftime("%b").upper()


def _contract(mock_mode, symbol="TCS", offset=200, month=None) -> tuple[str, float]:
    spot = mock_mode.spot(symbol)
    strike = round(spot / 50) * 50 + offset
    month = month or _expiry_month(2)
    return f"{symbol} {month} {int(strike)} CE", strike


def _run_check(client, contract: str) -> dict:
    resp = client.post("/api/v1/checks", json={"candidates": [{"text": contract}], "source": "kite"})
    assert resp.status_code == 200, resp.text
    return resp.json()["results"][0]


def _thesis_body(check: dict, **overrides) -> dict:
    entry = check["ask"] if check["ask"] is not None else check["premium"]
    body = {
        "check_id": check["id"], "mode": "paper", "why": "testing the option theses API end to end",
        # stop at 0.98x entry clears both settings' premium_stop_pct floor (0.6x) and keeps the ₹
        # risk small enough to stay under max_loss_per_trade regardless of strike, for mode="real" too.
        "target_premium": round(entry * 1.5, 2), "stop_premium": round(entry * 0.98, 2), "duration_days": 5,
        "lots": 1, "sector": "IT",
        # covers whichever verdict this mock strike happens to land on -- tests that care about
        # OVERRIDE_REQUIRED specifically construct their own body without this.
        "override_reason": "overriding whatever verdict this mock strike happens to get",
    }
    body.update(overrides)
    return body


def test_preview_returns_derived_numbers_without_saving(client, mock_mode):
    contract, _strike = _contract(mock_mode, offset=200)
    check = _run_check(client, contract)
    entry = check["ask"] if check["ask"] is not None else check["premium"]
    resp = client.post(
        "/api/v1/option-theses/preview",
        json={"contract": contract, "source": "kite", "target_premium": round(entry * 1.5, 2), "stop_premium": round(entry * 0.5, 2), "duration_days": 5, "lots": 1},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["reward_risk"] == pytest.approx(1.0, rel=0.05)
    assert "mkt_prob_target_first" in out
    assert "disaster_risk_per_lot" in out


def test_create_paper_thesis_success(client, mock_mode):
    contract, _strike = _contract(mock_mode, offset=200)
    check = _run_check(client, contract)
    resp = client.post("/api/v1/option-theses", json=_thesis_body(check))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "open"
    assert out["mode"] == "paper"
    assert out["data_source"] == "mock"
    assert "next" not in out


def test_create_real_thesis_returns_next_link(client, mock_mode):
    contract, _strike = _contract(mock_mode, offset=250)
    check = _run_check(client, contract)
    resp = client.post("/api/v1/option-theses", json=_thesis_body(check, mode="real"))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["next"] == f"/ui/order?check_id={check['id']}&thesis_id={out['id']}"


def test_duplicate_open_contract_is_409(client, mock_mode):
    contract, _strike = _contract(mock_mode, offset=300)
    check1 = _run_check(client, contract)
    resp1 = client.post("/api/v1/option-theses", json=_thesis_body(check1))
    assert resp1.status_code == 200, resp1.text

    check2 = _run_check(client, contract)
    resp2 = client.post("/api/v1/option-theses", json=_thesis_body(check2))
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "OPEN_THESIS_EXISTS"


def test_different_strike_on_the_same_underlying_is_fine(client, mock_mode):
    contract_a, _ = _contract(mock_mode, offset=0)
    contract_b, _ = _contract(mock_mode, offset=50)
    resp_a = client.post("/api/v1/option-theses", json=_thesis_body(_run_check(client, contract_a)))
    resp_b = client.post("/api/v1/option-theses", json=_thesis_body(_run_check(client, contract_b)))
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text
    assert resp_a.json()["id"] != resp_b.json()["id"]


def test_duration_pushing_past_the_hold_cutoff_is_422(client, mock_mode):
    """duration_days within [min,max] config bounds but whose end_date lands
    after the hold cutoff date must still be rejected (422 DURATION_TOO_LONG),
    distinct from an out-of-config-range duration (422 DURATION_OUT_OF_RANGE).
    Uses the *nearest* upcoming expiry (not the 2nd) so even a small,
    in-range duration_days reaches past its hold cutoff."""
    contract, _strike = _contract(mock_mode, offset=0, month=_expiry_month(1))
    check = _run_check(client, contract)
    resp = client.post("/api/v1/option-theses", json=_thesis_body(check, duration_days=2))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "DURATION_TOO_LONG"


def test_invalid_target_and_stop_are_422(client, mock_mode):
    contract, _strike = _contract(mock_mode, offset=350)
    check = _run_check(client, contract)
    entry = check["ask"] if check["ask"] is not None else check["premium"]

    resp = client.post("/api/v1/option-theses", json=_thesis_body(check, target_premium=round(entry * 0.9, 2)))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_TARGET"

    resp = client.post("/api/v1/option-theses", json=_thesis_body(check, stop_premium=round(entry * 1.1, 2)))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_STOP"


def test_duration_out_of_range_is_422(client, mock_mode):
    contract, _strike = _contract(mock_mode, offset=400)
    check = _run_check(client, contract)
    cfg = get_config()
    resp = client.post(
        "/api/v1/option-theses", json=_thesis_body(check, duration_days=cfg.option_theses.max_duration_days + 50),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] in ("DURATION_OUT_OF_RANGE", "DURATION_TOO_LONG")


def test_red_verdict_requires_override_reason(client, mock_mode):
    contract, _strike = _contract(mock_mode, offset=450)
    check = _run_check(client, contract)

    from optionguard.db.session import get_sessionmaker

    db = get_sessionmaker()()
    row = repo.get_check(db, check["id"])
    row.verdict = "red"
    db.commit()
    db.close()

    body_without_override = _thesis_body(check)
    body_without_override.pop("override_reason")
    resp = client.post("/api/v1/option-theses", json=body_without_override)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "OVERRIDE_REQUIRED"

    resp2 = client.post(
        "/api/v1/option-theses",
        json=_thesis_body(check, override_reason="I'm overriding this red verdict for a reason"),
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["verdict"] == "red"
    assert resp2.json()["override_id"] is not None


def test_get_list_and_notes(client, mock_mode):
    contract, _strike = _contract(mock_mode, offset=-50)
    check = _run_check(client, contract)
    created = client.post("/api/v1/option-theses", json=_thesis_body(check)).json()
    thesis_id = created["id"]

    get_resp = client.get(f"/api/v1/option-theses/{thesis_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["interim"]["days_left"] >= 0

    list_resp = client.get("/api/v1/option-theses", params={"status": "open"})
    assert any(t["id"] == thesis_id for t in list_resp.json())

    notes_resp = client.patch(f"/api/v1/option-theses/{thesis_id}/notes", json={"notes": "watching closely"})
    assert notes_resp.status_code == 200
    assert notes_resp.json()["notes"] == "watching closely"


def _csrf_headers_and_cookies(client):
    import re

    page = client.get("/settings")
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.cookies.update(page.cookies)
    return {"X-CSRF-Token": csrf_token}


def test_cancel_thesis(client, mock_mode):
    contract, _strike = _contract(mock_mode, offset=-100)
    check = _run_check(client, contract)
    thesis_id = client.post("/api/v1/option-theses", json=_thesis_body(check)).json()["id"]
    headers = _csrf_headers_and_cookies(client)

    resp = client.post(
        f"/ui/option-theses/{thesis_id}/cancel", json={"reason": "changed my mind about this one"}, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"

    resp2 = client.post(
        f"/ui/option-theses/{thesis_id}/cancel", json={"reason": "trying to cancel again here"}, headers=headers,
    )
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "NOT_OPEN"


def test_scorecard_empty_state(client, mock_mode):
    resp = client.get("/api/v1/option-theses/scorecard")
    assert resp.status_code == 200
    body = resp.json()
    assert "all" in body or "_summary" in body


def test_manual_exit_paper_thesis(client, mock_mode, db):
    contract, _strike = _contract(mock_mode, offset=-150)
    check = _run_check(client, contract)
    thesis = client.post("/api/v1/option-theses", json=_thesis_body(check)).json()
    row = repo.get_option_thesis(db, thesis["id"])

    exit_ts = row.created_at.replace(second=0, microsecond=0)
    entry = float(row.entry_premium)
    repo.upsert_option_minute_bar(
        db, instrument_token=row.opt_instrument_token, ts=exit_ts,
        o=entry, h=entry * 1.01, l=entry * 0.99, c=entry * 1.005, volume=100, oi=1000,
    )

    headers = _csrf_headers_and_cookies(client)
    resp = client.post(
        f"/ui/option-theses/{thesis['id']}/exit",
        json={"exit_time": exit_ts.isoformat(), "price": round(entry * 1.0, 2), "reason": "judgement", "text": "took it off early"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "closed"
    assert out["outcome"] == "manual"
    assert out["manual_exit_reason"] == "judgement: took it off early"


def test_manual_exit_price_out_of_bar_range_is_422(client, mock_mode, db):
    contract, _strike = _contract(mock_mode, offset=-200)
    check = _run_check(client, contract)
    thesis = client.post("/api/v1/option-theses", json=_thesis_body(check)).json()
    row = repo.get_option_thesis(db, thesis["id"])

    exit_ts = row.created_at.replace(second=0, microsecond=0)
    entry = float(row.entry_premium)
    repo.upsert_option_minute_bar(
        db, instrument_token=row.opt_instrument_token, ts=exit_ts,
        o=entry, h=entry * 1.01, l=entry * 0.99, c=entry * 1.005, volume=100, oi=1000,
    )

    headers = _csrf_headers_and_cookies(client)
    resp = client.post(
        f"/ui/option-theses/{thesis['id']}/exit",
        json={"exit_time": exit_ts.isoformat(), "price": round(entry * 2, 2), "reason": "news"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PRICE_OUT_OF_RANGE"


def test_link_trade_to_real_thesis(client, mock_mode, db):
    contract, _strike = _contract(mock_mode, offset=-250)
    check = _run_check(client, contract)
    resp_create = client.post("/api/v1/option-theses", json=_thesis_body(check, mode="real"))
    assert resp_create.status_code == 200, resp_create.text
    thesis = resp_create.json()
    row = repo.get_option_thesis(db, thesis["id"])

    trade = repo.create_trade(
        db, check_id=check["id"], entry_time=row.created_at, lots=row.lots, lot_size=row.lot_size,
        entry_premium=float(row.entry_premium) * 1.02, stop_premium=float(row.stop_premium),
        thesis_stop_level=None, time_stop_date=date(2027, 1, 1),
    )

    headers = _csrf_headers_and_cookies(client)
    resp = client.post(f"/ui/option-theses/{thesis['id']}/link-trade", json={"trade_id": trade.id}, headers=headers)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["trade_id"] == trade.id
    assert out["entry_premium"] == pytest.approx(float(trade.entry_premium))

    resp2 = client.post(f"/ui/option-theses/{thesis['id']}/link-trade", json={"trade_id": trade.id}, headers=headers)
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "THESIS_ALREADY_LINKED"
