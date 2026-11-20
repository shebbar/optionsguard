"""Section 7.8.8 / 7.8.9: MockKiteClient's deterministic order/GTT
simulation. No DB needed -- this is pure mock-client behaviour."""
from __future__ import annotations

from datetime import date

import pytest

from optionguard.market.mock_kite_client import MockKiteClient, monthly_expiries


@pytest.fixture
def option_rec():
    client = MockKiteClient(today=date(2026, 11, 17))
    client._ensure_built()
    expiry = monthly_expiries(client._today, count=2)[1]
    recs = [r for r in client.instruments("NFO") if r["name"] == "TCS" and r["expiry"] == expiry and r["instrument_type"] == "CE"]
    rec = recs[len(recs) // 2]
    quote = client.quote([f"NFO:{rec['tradingsymbol']}"])[f"NFO:{rec['tradingsymbol']}"]
    return client, rec, quote


def test_buy_limit_at_ask_fills_immediately(option_rec):
    client, rec, quote = option_rec
    ask = quote["depth"]["sell"][0]["price"]
    order_id = client.place_order(
        variety="regular", exchange="NFO", tradingsymbol=rec["tradingsymbol"], transaction_type="BUY",
        quantity=rec["lot_size"], product="NRML", order_type="LIMIT", price=ask,
    )
    latest = client.order_history(order_id)[-1]
    assert latest["status"] == "COMPLETE"
    assert latest["filled_quantity"] == rec["lot_size"]
    assert latest["average_price"] == ask


def test_buy_limit_far_below_ask_stays_open(option_rec):
    client, rec, quote = option_rec
    ask = quote["depth"]["sell"][0]["price"]
    order_id = client.place_order(
        variety="regular", exchange="NFO", tradingsymbol=rec["tradingsymbol"], transaction_type="BUY",
        quantity=rec["lot_size"], product="NRML", order_type="LIMIT", price=round(ask * 0.5, 2),
    )
    latest = client.order_history(order_id)[-1]
    assert latest["status"] == "OPEN"
    assert latest["filled_quantity"] == 0


def test_modify_price_up_to_ask_fills(option_rec):
    client, rec, quote = option_rec
    ask = quote["depth"]["sell"][0]["price"]
    order_id = client.place_order(
        variety="regular", exchange="NFO", tradingsymbol=rec["tradingsymbol"], transaction_type="BUY",
        quantity=rec["lot_size"], product="NRML", order_type="LIMIT", price=round(ask * 0.5, 2),
    )
    assert client.order_history(order_id)[-1]["status"] == "OPEN"
    client.modify_order(variety="regular", order_id=order_id, price=ask)
    assert client.order_history(order_id)[-1]["status"] == "COMPLETE"


def test_cancel_rejected_for_a_completed_order(option_rec):
    client, rec, quote = option_rec
    ask = quote["depth"]["sell"][0]["price"]
    order_id = client.place_order(
        variety="regular", exchange="NFO", tradingsymbol=rec["tradingsymbol"], transaction_type="BUY",
        quantity=rec["lot_size"], product="NRML", order_type="LIMIT", price=ask,
    )
    with pytest.raises(ValueError):
        client.cancel_order(variety="regular", order_id=order_id)


def test_cancel_open_order_succeeds(option_rec):
    client, rec, quote = option_rec
    ask = quote["depth"]["sell"][0]["price"]
    order_id = client.place_order(
        variety="regular", exchange="NFO", tradingsymbol=rec["tradingsymbol"], transaction_type="BUY",
        quantity=rec["lot_size"], product="NRML", order_type="LIMIT", price=round(ask * 0.5, 2),
    )
    client.cancel_order(variety="regular", order_id=order_id)
    assert client.order_history(order_id)[-1]["status"] == "CANCELLED"


def test_gtt_triggers_and_fills_when_marketable(option_rec):
    client, rec, quote = option_rec
    bid = quote["depth"]["buy"][0]["price"]
    trigger = round(bid * 1.05, 2)
    limit = round(trigger * 0.9, 2)  # well below bid -> marketable
    trigger_id = client.place_gtt(
        trigger_type="single", tradingsymbol=rec["tradingsymbol"], exchange="NFO",
        trigger_values=[trigger], last_price=bid,
        orders=[{"transaction_type": "SELL", "quantity": rec["lot_size"], "order_type": "LIMIT", "product": "NRML", "price": limit}],
    )
    gtt = client.get_gtt(trigger_id)
    assert gtt["status"] == "triggered"
    result = gtt["orders"][0]["result"]
    assert result["status"] == "COMPLETE"
    assert result["filled_quantity"] == rec["lot_size"]


def test_gtt_triggers_but_leg_stays_open_when_not_marketable(option_rec):
    client, rec, quote = option_rec
    bid = quote["depth"]["buy"][0]["price"]
    trigger = round(bid * 1.05, 2)
    limit = round(trigger * 2, 2)  # far above bid -> not marketable
    trigger_id = client.place_gtt(
        trigger_type="single", tradingsymbol=rec["tradingsymbol"], exchange="NFO",
        trigger_values=[trigger], last_price=bid,
        orders=[{"transaction_type": "SELL", "quantity": rec["lot_size"], "order_type": "LIMIT", "product": "NRML", "price": limit}],
    )
    gtt = client.get_gtt(trigger_id)
    assert gtt["status"] == "triggered"
    assert gtt["orders"][0]["result"]["status"] == "OPEN"


def test_gtt_not_yet_triggered_stays_active(option_rec):
    client, rec, quote = option_rec
    bid = quote["depth"]["buy"][0]["price"]
    trigger_id = client.place_gtt(
        trigger_type="single", tradingsymbol=rec["tradingsymbol"], exchange="NFO",
        trigger_values=[round(bid * 0.5, 2)], last_price=bid,
        orders=[{"transaction_type": "SELL", "quantity": rec["lot_size"], "order_type": "LIMIT", "product": "NRML", "price": round(bid * 0.45, 2)}],
    )
    assert client.get_gtt(trigger_id)["status"] == "active"


def test_delete_gtt(option_rec):
    client, rec, quote = option_rec
    bid = quote["depth"]["buy"][0]["price"]
    trigger_id = client.place_gtt(
        trigger_type="single", tradingsymbol=rec["tradingsymbol"], exchange="NFO",
        trigger_values=[round(bid * 0.5, 2)], last_price=bid,
        orders=[{"transaction_type": "SELL", "quantity": rec["lot_size"], "order_type": "LIMIT", "product": "NRML", "price": round(bid * 0.45, 2)}],
    )
    client.delete_gtt(trigger_id)
    assert client.get_gtt(trigger_id)["status"] == "deleted"


def test_get_gtts_lists_all_and_has_gtt_sees_active_ones(option_rec):
    client, rec, quote = option_rec
    bid = quote["depth"]["buy"][0]["price"]
    client.place_gtt(
        trigger_type="single", tradingsymbol=rec["tradingsymbol"], exchange="NFO",
        trigger_values=[round(bid * 0.5, 2)], last_price=bid,
        orders=[{"transaction_type": "SELL", "quantity": rec["lot_size"], "order_type": "LIMIT", "product": "NRML", "price": round(bid * 0.45, 2)}],
    )
    gtts = client.get_gtts()
    assert len(gtts) == 1
    assert gtts[0]["condition"]["tradingsymbol"] == rec["tradingsymbol"]
