"""KiteProvider against the offline MockKiteClient fixture -- no live calls."""
from __future__ import annotations

import pytest

from optionguard.market.base import NotAvailable
from optionguard.market.kite import KiteProvider
from optionguard.market.mock_kite_client import MockKiteClient


@pytest.fixture
def provider() -> KiteProvider:
    # No frozen `today` here on purpose: KiteProvider.history() derives its
    # date range from the real date.today(), not the client's injected
    # "as of" date, so freezing MockKiteClient to a fixed past date would
    # make history()'s tail silently drift out of sync with spot() as real
    # time passes (same default behavior as production, where neither is
    # frozen either).
    return KiteProvider(MockKiteClient(api_key="test"))


def test_resolve_expiry_is_a_listed_thursday(provider):
    expiry = provider.resolve_expiry("TCS", "NOV")
    assert expiry.month == 11
    assert expiry.weekday() == 3  # Thursday


def test_resolve_expiry_unknown_symbol_raises_not_available(provider):
    with pytest.raises(NotAvailable):
        provider.resolve_expiry("NOTASYMBOL", "NOV")


def test_spot_and_quote_consistent(provider):
    expiry = provider.resolve_expiry("TCS", "NOV")
    spot = provider.spot("TCS")
    strike = round(spot / 50) * 50
    quote = provider.option_quote("TCS", expiry, strike, "CE")
    assert quote.last_price > 0
    assert quote.lot_size == 175
    assert quote.bid < quote.last_price < quote.ask


def test_chain_returns_both_sides(provider):
    expiry = provider.resolve_expiry("TCS", "NOV")
    spot = provider.spot("TCS")
    strike = round(spot / 50) * 50
    quotes = provider.chain("TCS", expiry, [strike - 50, strike, strike + 50])
    types = {q.opt_type for q in quotes}
    assert types == {"CE", "PE"}
    assert len(quotes) == 6


def test_history_matches_latest_spot(provider):
    bars = provider.history("TCS", 30)
    assert len(bars) == 30
    assert bars[-1].close == pytest.approx(provider.spot("TCS"), abs=0.01)


def test_history_raises_for_option_token(provider):
    with pytest.raises(NotAvailable):
        provider.history("NOTASYMBOL", 30)


def test_list_strikes_nonempty_and_sorted(provider):
    expiry = provider.resolve_expiry("TCS", "NOV")
    strikes = provider.list_strikes("TCS", expiry)
    assert strikes == sorted(strikes)
    assert len(strikes) > 0


def test_has_gtt_false_when_none_exist(provider):
    expiry = provider.resolve_expiry("TCS", "NOV")
    strike = round(provider.spot("TCS") / 50) * 50
    assert provider.has_gtt("TCS", expiry, strike, "CE") is False


def test_has_gtt_true_when_matching_active_gtt_exists(monkeypatch, provider):
    expiry = provider.resolve_expiry("TCS", "NOV")
    strike = round(provider.spot("TCS") / 50) * 50
    rec = provider._find_option("TCS", expiry, strike, "CE")

    monkeypatch.setattr(
        provider.client, "get_gtts",
        lambda: [{"status": "active", "condition": {"tradingsymbol": rec["tradingsymbol"]}}],
    )
    assert provider.has_gtt("TCS", expiry, strike, "CE") is True


def test_has_gtt_ignores_inactive_gtt(monkeypatch, provider):
    expiry = provider.resolve_expiry("TCS", "NOV")
    strike = round(provider.spot("TCS") / 50) * 50
    rec = provider._find_option("TCS", expiry, strike, "CE")

    monkeypatch.setattr(
        provider.client, "get_gtts",
        lambda: [{"status": "triggered", "condition": {"tradingsymbol": rec["tradingsymbol"]}}],
    )
    assert provider.has_gtt("TCS", expiry, strike, "CE") is False


def test_deterministic_across_instances(provider):
    other = KiteProvider(MockKiteClient(api_key="test"))
    assert provider.spot("TCS") == other.spot("TCS")
