"""Computes every metric in DESIGN.md section 7.1 for one candidate. No
verdict/sizing decisions here -- see verdict.py and sizing.py."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

from optionguard.config import Config
from optionguard.market.base import DailyBar, MarketProvider, NotAvailable, OptionQuote
from optionguard.pricing import bs, iv as iv_mod, probability as prob
from optionguard.pricing.calendar import trading_days_to_expiry, year_fraction

HV_PERCENTILE_WINDOW = 252
HV_WINDOW_SHORT = 20
HV_WINDOW_LONG = 60
MIN_DELTA_FOR_DAILY_MOVE = 0.05  # per DESIGN.md 7.6


@dataclass(slots=True)
class CandidateRequest:
    symbol: str
    expiry: date
    strike: float
    opt_type: str
    data_source: str  # "manual" | "mock" | "kite" -- "mock" is what callers who asked
    # for source="kite" actually get tagged with until real Kite credentials exist;
    # alternatives/OI logic below treats "mock" the same as "kite" (both != "manual")
    thesis_target: float | None = None
    thesis_date: date | None = None
    manual_hv20: float | None = None


@dataclass(slots=True)
class Metrics:
    spot: float
    premium: float
    bid: float | None
    ask: float | None
    lot_size: int
    iv: float | None
    iv_from_ltp: bool
    hv20: float | None
    hv60: float | None
    hv_percentile: float | None
    breakeven: float
    breakeven_pct: float
    expected_move_pct: float | None
    be_to_move_ratio: float | None
    prob_profit: float | None
    prob_zero: float | None
    prob_thesis: float | None
    theta_per_day_lot: float | None
    theta_final_2w_lot: float | None
    delta: float | None
    daily_move_needed: float | None
    daily_move_needed_pct: float | None
    stretch_5d_pct: float | None
    stretch_20dma_pct: float | None
    spread_pct: float | None
    volume: int
    oi: int
    earnings_before_expiry: bool
    trading_days_to_expiry: int


def _log_returns(closes: list[float]) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    return np.diff(np.log(arr))


def _annualized_vol(log_returns: np.ndarray, trading_days_per_year: int) -> float | None:
    if len(log_returns) < 2:
        return None
    return float(np.std(log_returns, ddof=1) * np.sqrt(trading_days_per_year))


def _hv_series(bars: list[DailyBar], window: int, trading_days_per_year: int) -> np.ndarray:
    closes = [b.close for b in bars]
    rets = _log_returns(closes)
    if len(rets) < window:
        return np.array([])
    out = []
    for i in range(window, len(rets) + 1):
        out.append(np.std(rets[i - window : i], ddof=1) * np.sqrt(trading_days_per_year))
    return np.asarray(out)


def _escrowed_dividend_adjustment(
    spot: float, dividends: list[tuple[date, float]], today: date, r: float, holidays: set
) -> float:
    adj = spot
    for div_date, amount in dividends:
        if amount is None or div_date < today:
            continue
        t_div = year_fraction(trading_days_to_expiry(today, div_date, holidays))
        adj -= amount * np.exp(-r * t_div)
    return adj


def _pick_iv_price(quote: OptionQuote) -> tuple[float, bool]:
    """Returns (price_used, iv_from_ltp). Mid of bid/ask when both exist and
    the spread is <= 20% of mid; otherwise the last traded price."""
    if quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask > 0:
        mid = (quote.bid + quote.ask) / 2
        spread = (quote.ask - quote.bid) / mid if mid else None
        if spread is not None and spread <= 0.20:
            return mid, False
    return quote.last_price, True


def compute_metrics(
    provider: MarketProvider,
    req: CandidateRequest,
    *,
    config: Config,
    today: date,
    dividends: list[tuple[date, float]] | None = None,
    has_earnings_before_expiry: bool = False,
) -> Metrics:
    quote = provider.option_quote(req.symbol, req.expiry, req.strike, req.opt_type)
    spot = provider.spot(req.symbol)
    r = config.pricing.risk_free_rate
    trading_days_per_year = config.pricing.trading_days_per_year
    holidays = set(config.holidays.dates)

    days_to_expiry = trading_days_to_expiry(today, req.expiry, holidays)
    T = year_fraction(max(days_to_expiry, 0) or 1, trading_days_per_year)
    S_adj = _escrowed_dividend_adjustment(spot, dividends or [], today, r, holidays)

    price_for_iv, iv_from_ltp = _pick_iv_price(quote)
    sigma = iv_mod.implied_vol(price_for_iv, S_adj, req.strike, T, r, req.opt_type)

    try:
        bars = provider.history(req.symbol, HV_PERCENTILE_WINDOW + HV_WINDOW_LONG + 5)
    except NotAvailable:
        bars = []

    hv20 = hv60 = hv_percentile = None
    stretch_5d_pct = stretch_20dma_pct = None
    if bars:
        closes = [b.close for b in bars]
        rets = _log_returns(closes)
        hv20 = _annualized_vol(rets[-HV_WINDOW_SHORT:], trading_days_per_year) if len(rets) >= HV_WINDOW_SHORT else None
        hv60 = _annualized_vol(rets[-HV_WINDOW_LONG:], trading_days_per_year) if len(rets) >= HV_WINDOW_LONG else None
        series = _hv_series(bars, HV_WINDOW_SHORT, trading_days_per_year)
        if hv20 is not None and len(series) >= 2:
            hv_percentile = float((series <= hv20).sum() / len(series) * 100)
        if len(closes) >= 6:
            stretch_5d_pct = (closes[-1] - closes[-6]) / closes[-6]
        if len(closes) >= 20:
            sma20 = float(np.mean(closes[-20:]))
            stretch_20dma_pct = (spot - sma20) / sma20
    elif req.manual_hv20 is not None:
        hv20 = req.manual_hv20

    breakeven = req.strike + quote.last_price if req.opt_type == "CE" else req.strike - quote.last_price
    breakeven_pct = (breakeven - spot) / spot if spot else 0.0

    expected_move_pct = prob.expected_move_pct(sigma, T) if sigma is not None else None
    be_to_move_ratio = (
        abs(breakeven_pct) / expected_move_pct if expected_move_pct else None
    )

    prob_profit = prob_zero = None
    if sigma is not None:
        prob_profit = prob.prob_profit(S_adj, breakeven, T, r, sigma, req.opt_type)
        prob_zero = prob.prob_zero(S_adj, req.strike, T, r, sigma, req.opt_type)

    prob_thesis = None
    if sigma is not None and req.thesis_target is not None and req.thesis_date is not None:
        thesis_days = trading_days_to_expiry(today, req.thesis_date, holidays)
        T_thesis = year_fraction(max(thesis_days, 1), trading_days_per_year)
        prob_thesis = prob.prob_touch(S_adj, req.thesis_target, T_thesis, r, sigma)

    theta_per_day_lot = theta_final_2w_lot = None
    delta = daily_move_needed = daily_move_needed_pct = None
    if sigma is not None:
        g = bs.greeks(S_adj, req.strike, T, r, sigma, req.opt_type, trading_days_per_year)
        theta_per_day_lot = g["theta_per_day"] * quote.lot_size
        T2 = year_fraction(10, trading_days_per_year)
        g2 = bs.greeks(spot, req.strike, T2, r, sigma, req.opt_type, trading_days_per_year)
        theta_final_2w_lot = g2["theta_per_day"] * quote.lot_size

        delta = g["delta"]
        if abs(delta) >= MIN_DELTA_FOR_DAILY_MOVE:
            daily_move_needed = abs(g["theta_per_day"]) / abs(delta)
            daily_move_needed_pct = daily_move_needed / spot if spot else None

    spread_pct = None
    if quote.bid is not None and quote.ask is not None:
        mid = (quote.bid + quote.ask) / 2
        if mid:
            spread_pct = (quote.ask - quote.bid) / mid

    return Metrics(
        spot=spot,
        premium=quote.last_price,
        bid=quote.bid,
        ask=quote.ask,
        lot_size=quote.lot_size,
        iv=sigma,
        iv_from_ltp=iv_from_ltp,
        hv20=hv20,
        hv60=hv60,
        hv_percentile=hv_percentile,
        breakeven=breakeven,
        breakeven_pct=breakeven_pct,
        expected_move_pct=expected_move_pct,
        be_to_move_ratio=be_to_move_ratio,
        prob_profit=prob_profit,
        prob_zero=prob_zero,
        prob_thesis=prob_thesis,
        theta_per_day_lot=theta_per_day_lot,
        theta_final_2w_lot=theta_final_2w_lot,
        delta=delta,
        daily_move_needed=daily_move_needed,
        daily_move_needed_pct=daily_move_needed_pct,
        stretch_5d_pct=stretch_5d_pct,
        stretch_20dma_pct=stretch_20dma_pct,
        spread_pct=spread_pct,
        volume=quote.volume,
        oi=quote.oi,
        earnings_before_expiry=has_earnings_before_expiry,
        trading_days_to_expiry=days_to_expiry,
    )
