"""Section 7.9.10: the server-rendered /option-theses screen -- the page
renders without a Jinja error, the new-thesis panel's preview/create HTMX
endpoints work, and an open thesis's interim progress (days left, live
premium) is embedded correctly."""
from __future__ import annotations

import re
from datetime import date

import pytest

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


def _run_check(client, contract: str) -> dict:
    resp = client.post("/api/v1/checks", json={"candidates": [{"text": contract}], "source": "kite"})
    assert resp.status_code == 200, resp.text
    return resp.json()["results"][0]


def test_page_renders_with_tab_link(client, mock_mode):
    resp = client.get("/option-theses")
    assert resp.status_code == 200
    assert "New option thesis" in resp.text
    assert 'href="/option-theses"' in resp.text  # nav link present on every page
    home = client.get("/")
    assert 'href="/option-theses"' in home.text


def test_preview_html_renders_derived_numbers(client, mock_mode):
    spot = mock_mode.spot("TCS")
    strike = round(spot / 50) * 50 + 200
    contract = f"TCS {_expiry_month(2)} {int(strike)} CE"
    check = _run_check(client, contract)
    entry = check["ask"] if check["ask"] is not None else check["premium"]

    resp = client.post(
        "/option-theses/preview-html",
        data={
            "contract": contract, "source": "kite", "target_premium": round(entry * 1.5, 2),
            "stop_premium": round(entry * 0.9, 2), "duration_days": 5, "lots": 1,
        },
    )
    assert resp.status_code == 200, resp.text
    assert "error" not in resp.text.lower()
    assert "Reward:risk" in resp.text
    assert "Market-implied odds" in resp.text


def test_create_then_open_row_shows_interim_progress(client, mock_mode, db):
    from optionguard.db import repo

    spot = mock_mode.spot("TCS")
    strike = round(spot / 50) * 50 + 250
    contract = f"TCS {_expiry_month(2)} {int(strike)} CE"
    check = _run_check(client, contract)
    entry = check["ask"] if check["ask"] is not None else check["premium"]

    resp = client.post(
        "/option-theses-html",
        data={
            "check_id": check["id"], "mode": "paper", "why": "web route verification, not a real trade idea",
            "target_premium": round(entry * 1.5, 2), "stop_premium": round(entry * 0.9, 2), "duration_days": 5,
            "lots": 1, "sector": "IT", "override_reason": "overriding whatever verdict this strike gets",
        },
    )
    assert resp.status_code == 200, resp.text
    thesis_id = int(re.search(r"Thesis #(\d+) saved", resp.text).group(1))
    row = repo.get_option_thesis(db, thesis_id)
    assert row.status == "open"

    page = client.get("/option-theses")
    assert resp.status_code == 200
    assert f"#{thesis_id}" in page.text
    assert "Days left" in page.text
    # the interim block must always carry a live_premium key (None if the quote
    # failed) -- this used to be an Undefined-vs-None Jinja bug that rendered blank
    # instead of "n/a" when a quote genuinely couldn't be fetched.
    detail = client.get(f"/api/v1/option-theses/{thesis_id}").json()
    assert "live_premium" in detail["interim"]
    assert detail["interim"]["days_left"] == 5


def test_scorecard_html_partial_renders(client, mock_mode):
    resp = client.get("/option-theses/scorecard-html")
    assert resp.status_code == 200
    assert "No scorecard data yet" in resp.text or "n=" in resp.text


def test_checks_html_card_carries_a_data_check_id_for_the_option_theses_page_to_read(client, mock_mode):
    """The Option theses page's "Run check" button swaps this same partial in,
    then reads data-check-id client-side to fill the hidden check_id field --
    see option_theses.html's hx-on::after-swap handler."""
    spot = mock_mode.spot("TCS")
    strike = round(spot / 50) * 50 + 200
    contract = f"TCS {_expiry_month(2)} {int(strike)} CE"
    resp = client.post("/checks-html", data={"text": contract, "source": "kite"})
    assert resp.status_code == 200
    match = re.search(r'data-check-id="(\d+)"', resp.text)
    assert match is not None
    check = _run_check(client, contract)
    # the two checks are different rows (each /checks call creates a new one), but
    # both must be present and non-empty -- the card always carries a real id.
    assert int(match.group(1)) > 0
    assert check["id"] > 0


def test_create_without_check_id_gives_a_friendly_error_not_a_crash(client, mock_mode):
    """Regression: submitting the new-thesis form before "Run check" populated
    the hidden check_id field used to crash with a raw
    "invalid literal for int() with base 10: ''" instead of a usable message."""
    resp = client.post(
        "/option-theses-html",
        data={
            "check_id": "", "mode": "paper", "why": "reproduces the empty check_id bug report",
            "target_premium": "120", "stop_premium": "65", "duration_days": "5", "lots": "1",
        },
    )
    assert resp.status_code == 200
    assert "error" in resp.text.lower()
    assert "invalid literal" not in resp.text
    assert "Run a check" in resp.text
