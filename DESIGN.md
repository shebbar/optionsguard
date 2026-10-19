# OptionGuard — Design Spec

A local, single-user tool that evaluates long stock-option trades (NSE, monthly expiry) **before** entry, enforces personal risk rules, monitors open positions, and keeps a journal. Exposed through (a) a local web UI and (b) an MCP server for Claude Desktop.

**Goal:** stop the "4 small wins, 1 wipeout" pattern. The app does not predict direction. It makes the odds, cost and risk of each trade explicit, caps position size, and forces exits.

> **v1 implementation notes (read this first).** The build at `~/apps/optionsguard` follows
> this spec with four deliberate deviations, kept here so this doc stays accurate for
> anyone (including Claude Desktop) reading it alongside the code:
> 1. **Postgres, not SQLite.** The DB lives in an existing local Postgres container
>    (`pgdb`), in its own `optionguard` database, via SQLAlchemy 2.x + `psycopg` + Alembic.
> 2. **A mock Kite client stands in for `kiteconnect`** until real Zerodha API credentials
>    are configured (see section 8). It mirrors the real client's method names and
>    response shapes exactly, so the switch to live data is a credential change, not a
>    rewrite. The *request* still only ever says `source: "manual"` or `source: "kite"` --
>    callers don't need to know or care whether "kite" is mock-backed. But the *stored and
>    returned* `data_source` is three-valued: `"manual" | "mock" | "kite"`. A `"kite"`
>    request that had no real credentials at the time gets permanently tagged
>    `data_source = "mock"`, not `"kite"` -- this distinction matters once real credentials
>    are added, so old simulated checks never get mistaken for real market history in the
>    journal. `GET /status` reports `kite_mode: "mock" | "live"`, and the UI shows a MOCK
>    DATA banner plus a per-card/row tag whenever something is mock-backed. (An earlier
>    version of this build conflated mock and real under the same `"kite"` tag -- fixed
>    after a design review caught it.)
> 3. **The monitor loop and Telegram alerting (section 12) are not implemented in v1** --
>    removed by request, to be added later if needed. The Positions screen computes live
>    premium/spot/P&L on demand per request instead of via a background poller, and there
>    is no `alerts` table. In its place, `GET /positions` runs a read-only **GTT check**:
>    for each kite/mock position it asks Kite whether a matching GTT order actually exists
>    (`KiteProvider.has_gtt`), and flags it if not -- since otherwise nothing in this build
>    enforces a stop; it's just a number in the database until you place a real order.
> 4. **Kite rate limiting is implemented** (`KiteProvider._TokenBucket`, ≤3 req/sec on
>    `quote()`/`ltp()`), matching section 8 below -- flagging this explicitly since it was
>    initially missed and added back after a review.
>
> Sections 3, 4, 8, 10, 11, 12, 13, 15 and 17 below are annotated inline where they differ
> from what's running.

---

## 1. Principles (non-negotiable)

1. **All rules are enforced in the core service**, never in the UI or the LLM. Any caller (web UI, MCP, script) hits the same checks.
2. **All numbers are computed in code.** The MCP layer returns structured data; Claude only explains it.
3. **Rules can be tightened anywhere, loosened only in the web UI**, and loosened settings take effect the **next trading day**.
4. **Every candidate is logged**, traded or not, so outcomes can be compared later.
5. **Every override needs a typed reason**, stored in `overrides`.
6. **v1 places no orders.** The user places orders in Kite; the app records and monitors them.

---

## 2. Scope

### v1 (build now)
- Pricing engine: Black-Scholes, implied volatility, probabilities, greeks.
- `check` flow with two data sources: **manual** input and **Kite Connect**.
- Verdict engine (red / amber / green) with config-driven thresholds.
- Position sizing from risk rules.
- Single-leg alternative strikes.
- Trade recording, stop tracking, exit recording.
- ~~Monitor loop during market hours plus Telegram alerts.~~ **Removed from v1 build** --
  see the implementation note above. Live P&L on the Positions screen is computed
  on-demand per request instead.
- Journal and stats.
- Web UI with Check, Positions, Journal and Settings screens.
- MCP server (stdio) for Claude Desktop.

### v2 (later, not in this build)
- Debit-spread alternatives (behind the `show_spreads` setting).
- IV percentile from historical option data (NSE bhavcopy backfill).
- Calibration backtest module (bhavcopy based).
- Optional automated exits through Kite. Check SEBI's retail-algo requirements first, e.g. static IP and registration.
- WebSocket ticks (KiteTicker) instead of polling.

---

## 3. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12, managed with `uv` |
| Core service | FastAPI + Uvicorn, bound to `127.0.0.1` only |
| DB | **PostgreSQL** (not SQLite -- v1 build uses an existing local `pgdb` container) via SQLAlchemy 2.x (`psycopg` driver); migrations with Alembic |
| Maths | `numpy`, `scipy` (`norm`, `brentq`) |
| Broker | `kiteconnect` (official Python client), **fronted by an offline mock client until real API credentials exist** -- see section 8 |
| Web UI | Server-rendered Jinja2 + HTMX + minimal CSS (no SPA) |
| MCP | Official `mcp` Python SDK, stdio transport. **Note:** `mcp` 2.x renamed `FastMCP` to `MCPServer` (`from mcp.server.mcpserver import MCPServer`) -- the v1 build targets that API. |
| ~~Scheduling~~ | ~~APScheduler inside the core service~~ -- **not used in v1**; monitor loop removed |
| ~~Notifications~~ | ~~Telegram Bot API~~ -- **not implemented in v1**; alerting removed |
| Tests | `pytest`, against a throwaway `<db>_test` Postgres database |
| Config | `config.toml` (thresholds, holidays) + `.env` (secrets) |

---

## 4. Architecture

```
Claude Desktop ──stdio──► mcp_server (thin HTTP client)
                                   │
Browser ──HTTP──► core service (FastAPI, 127.0.0.1:8787)
                     ├── pricing/      pure maths, no I/O
                     ├── analysis/     metrics, verdict, sizing, alternatives
                     ├── rules/        guardrails (single enforcement point)
                     ├── market/       data providers: manual | kite (kite.py wraps
                     │                 either mock_kite_client.py or a real
                     │                 kiteconnect.KiteConnect -- same interface)
                     ├── journal/      stats queries
                     ├── web/          Jinja + HTMX views
                     └── db/           models, repo, migrations
                                   │
                              PostgreSQL
```

(`monitor/` from the original design -- scheduled jobs, alerts -- is not present in the
v1 build; see the implementation note at the top of this doc.)

- The MCP server **does not import the core or touch the DB**. It calls the core's JSON API. If the core isn't running, the tools return a clear error: "Start OptionGuard: `uv run optionguard serve`".
- Only the core process talks to Kite and holds the access token.

### Project layout

```
optionguard/
  pyproject.toml
  config.toml
  .env.example
  alembic/
  src/optionguard/
    __init__.py
    cli.py                 # `optionguard serve`, `optionguard backfill-settle`
    config.py              # loads config.toml + .env (pydantic-settings)
    app.py                 # FastAPI app factory (binds 127.0.0.1 only -- no scheduler in v1)
    db/{models.py, repo.py, session.py}
    pricing/{bs.py, iv.py, probability.py, calendar.py}
    market/{base.py, manual.py, kite.py, mock_kite_client.py, factory.py, symbols.py}
    analysis/{metrics.py, verdict.py, sizing.py, alternatives.py, oi.py, explain.py}
    rules/{guardrails.py, errors.py}
    journal/stats.py
    api/{checks.py, trades.py, settings.py, events.py, journal.py, kite_auth.py, status.py,
         schemas.py, serializers.py, errors.py}   # last three weren't in the original list
    web/{routes.py, csrf.py, templates/, static/}
  mcp_server/
    server.py              # MCPServer tools → HTTP calls to core (mcp 2.x; see section 3)
  tests/
```

---

## 5. Domain conventions

- **Instruments:** NSE stock options, monthly expiry, European exercise, physically settled.
- **User input syntax:** `SYMBOL MON STRIKE CE|PE`, e.g. `TCS NOV 2500 CE`. `MON` resolves to the next listed expiry in that month. Optional thesis: `@2550 by 2026-11-10`.
- **Kite tradingsymbol:** resolve via the `kite.instruments("NFO")` dump, cached daily. Match on `name`, `expiry`, `strike` and `instrument_type`. **Do not construct symbols by string formatting**, and do not hardcode expiry weekdays; take the expiry from the dump.
- **Lot size:** from the instruments dump (Kite), or user-supplied (manual).
- **Time:** use trading days. `T = trading_days_to_expiry / 252`. A trading-day calendar comes from `config.toml [holidays]`, an NSE holiday list the user maintains. Use the same `T` convention for IV extraction and for projections.
- **Risk-free rate:** `config.toml [pricing] risk_free_rate`. The user sets it to the current 91-day T-bill yield.
- **Dividends:** if a `corporate_events` dividend falls before expiry, use the escrowed-dividend model: `S_adj = S − D·e^(−r·t_div)`.
- **Timezone:** all timestamps are stored in UTC and displayed in IST (`Asia/Kolkata`). Market hours are 09:15–15:30 IST.

---

## 6. Pricing engine (`pricing/`)

Pure functions only, with no I/O. All of them are unit-tested.

- `bs_price(S, K, T, r, sigma, opt_type) -> float`
- `greeks(S, K, T, r, sigma, opt_type) -> {delta, gamma, theta_per_day, vega}`. `theta_per_day` is per trading day.
- `implied_vol(price, S, K, T, r, opt_type) -> float | None`
  - Brent's method on [0.01, 3.0].
  - Return `None` if the price is below intrinsic or no root is found. The caller flags this as "price inconsistent / stale".
  - In Kite mode, use the **mid** of bid/ask when both exist and the spread is ≤ 20% of mid; otherwise use the last traded price and set the flag `iv_from_ltp`.
- `prob_above(S, X, T, r, sigma) -> float`: lognormal probability that S_T > X, i.e. `N(d2)` with K = X.
- `prob_profit`: for a CE, `prob_above(breakeven)`; for a PE, `1 − prob_above(breakeven)`.
- `prob_zero`: probability the option expires worthless. For a CE, `1 − prob_above(K)`; for a PE, `prob_above(K)`.
- `prob_touch(X)`: approximately `min(1, 2 × P(finish beyond X))`. Used only for the thesis target.
- `expected_move_pct = sigma × sqrt(T)`, i.e. one standard deviation.

Note in the UI and docs: these are risk-neutral probabilities implied by market prices, not forecasts.

---

## 7. Analysis (`analysis/`)

### 7.1 Metrics computed per candidate

| Field | Definition |
|---|---|
| `spot`, `premium`, `bid`, `ask`, `lot_size` | From the provider |
| `iv` | Section 6 |
| `hv20`, `hv60` | Annualised realised volatility of daily log returns (needs history; `None` in manual mode unless the user supplies it) |
| `iv_hv_ratio` | `iv / hv20` |
| `hv_percentile` | Percentile of current `hv20` within the trailing 252-day `hv20` series (stands in for IV percentile in v1) |
| `breakeven` | CE: `K + premium`; PE: `K − premium` |
| `breakeven_pct` | `(breakeven − spot) / spot`, signed |
| `expected_move_pct` | Section 6 |
| `be_to_move_ratio` | `abs(breakeven_pct) / expected_move_pct` |
| `prob_profit`, `prob_zero` | Section 6 |
| `prob_thesis` | If a thesis target is given, `prob_touch(target)` by the thesis date |
| `theta_per_day_lot` | `theta_per_day × lot_size` (₹) |
| `theta_final_2w_lot` | Theta per lot recomputed with T = 10 trading days at the current spot |
| `stretch_5d_pct` | 5-day % change of the underlying |
| `stretch_20dma_pct` | % distance of spot from its 20-day moving average |
| `spread_pct` | `(ask − bid) / mid` |
| `volume`, `oi` | From the provider |
| `earnings_before_expiry` | From `corporate_events` |
| `oi_context` | Section 7.5 (Kite mode only) |

### 7.2 Verdict engine (`verdict.py`)

The engine produces a list of `Flag(code, level, message)`, where level is `red` or `amber`. The verdict is **red** if any flag is red, **amber** if any flag is amber, otherwise **green**. All thresholds live in `config.toml [verdict]`.

Default rules:

| Code | Condition | Level |
|---|---|---|
| `LOW_PROB` | `prob_profit < 0.25` | red |
| `LOW_PROB_SOFT` | `0.25 ≤ prob_profit < 0.35` | amber |
| `FAR_BREAKEVEN` | `be_to_move_ratio > 0.8` | red |
| `CHASING` | `stretch_5d_pct ≥ 8%` in the trade's direction (or `stretch_20dma_pct ≥ 7%`) | amber |
| `EXPENSIVE_VOL` | `iv_hv_ratio > 1.3` or `hv_percentile > 80` | amber |
| `WIDE_SPREAD` | `spread_pct > 10%` | red; > 5% amber |
| `ILLIQUID` | `volume < 50` contracts or `oi < 200` contracts | amber |
| `EARNINGS_INSIDE` | Earnings before expiry | amber, with a message about the volatility drop after results |
| `NEAR_EXPIRY` | `< 8` trading days to expiry | red |
| `IV_UNRELIABLE` | `iv is None` or `iv_from_ltp` | amber |
| `OI_WALL` | Buying a CE with strike above the max-call-OI strike (or a PE below the max-put-OI strike) | amber |
| `TOO_BIG_FOR_CAP` | `max_lots == 0` | red |

### 7.3 Sizing (`sizing.py`)

```
loss_per_lot_at_stop = premium × lot_size × premium_stop_pct
full_loss_per_lot    = premium × lot_size
max_lots = min(
  floor(max_loss_per_trade / loss_per_lot_at_stop),
  floor(2 × max_loss_per_trade / full_loss_per_lot)   # protects against gaps through the stop
)
```

### 7.4 Alternatives (`alternatives.py`)

- Kite mode only. Consider the same expiry and type, up to 6 strikes between the requested strike and at-the-money.
- Compute the full metrics for each and return the **top 2** by `prob_profit` among those with `verdict != red` and `max_lots ≥ 1`.
- In manual mode, return an empty list with the note "connect Kite for alternatives".
- Spreads are v2.

### 7.5 OI context (`oi.py`)

- Kite mode: fetch quotes for strikes within ±10% of spot (one `kite.quote` call, ≤ 500 instruments).
- Return `max_call_oi_strike`, `max_put_oi_strike`, `pcr_oi`, and the top 3 strikes by OI for each side.
- This is descriptive only. The UI copy must say it is context, not a signal.

---

### 7.6 Holding-period metrics (added)

Purpose: most trades are exited within days, not held to expiry. Show what short
holding periods look like.

**Daily move needed**
- `delta` = Black-Scholes delta at entry (already computed in greeks).
- `daily_move_needed = abs(theta_per_day_share) / abs(delta)`, in ₹ of the
  underlying per trading day. Also `daily_move_needed_pct = daily_move_needed / spot`.
- If `abs(delta) < 0.05`, set both to None and add the amber flag `DELTA_TOO_SMALL`.
- Card copy: "Stock must move ₹X (Y%) your way per day just to offset decay."

**Scenario grid**
- Days held d ∈ {1, 3, 5} trading days; underlying move m ∈ {−2%, −1%, 0, +1%, +2%}.
- Entry price = ask if known, else premium.
- Estimated exit value = `bs_price(spot × (1+m), K, T − d/252, r, iv, opt_type)`
  − (half the spread if bid/ask known, else 0).
- P&L per lot = (exit value − entry) × lot_size. IV is held constant.
  If an earnings date falls within d, add the note "IV drop after results not modelled".
- Skip any d where T − d/252 ≤ 0.

**Outputs**
- `CheckResult` gains `delta`, `daily_move_needed`, `daily_move_needed_pct`,
  `scenarios: [{days, move_pct, est_value, pnl_per_lot}]`.
- Store `delta` and `daily_move_needed` as columns on `checks`; scenarios go in `raw`.
- `explanation_facts` gains the daily-move sentence.
- Web UI: a 3×5 grid under the metrics, P&L cells tinted by sign.
- MCP: included in `check_options` results; no new tool.

**Tests**
- `daily_move_needed` equals |theta| / |delta| for a known option.
- Zero move after 1 day gives est_value < entry (decay plus spread).
- For a PE, P&L increases as m goes more negative; for a CE, as m goes more positive.
- `delta < 0.05` gives None and the `DELTA_TOO_SMALL` flag.

## 8. Market data providers (`market/`)

```python
class MarketProvider(Protocol):
    def option_quote(self, symbol, expiry, strike, opt_type) -> OptionQuote
    def spot(self, symbol) -> float
    def history(self, symbol, days: int) -> list[DailyBar]   # may raise NotAvailable
    def chain(self, symbol, expiry, strikes: list[float]) -> list[OptionQuote]  # may raise NotAvailable
    def resolve_expiry(self, symbol, month: str) -> date
    def lot_size(self, symbol, expiry) -> int
```

- **ManualProvider:** values come from the request payload (`spot`, `premium`, optional `bid`/`ask`, `lot_size`, `expiry`, optional `hv20`). `history` and `chain` raise `NotAvailable`, and the dependent metrics become `None` with the flag noted.
- **KiteProvider:** implemented in `market/kite.py` against a client *interface*
  (`instruments`/`quote`/`ltp`/`historical_data`/`login_url`/`generate_session`), not
  directly against `kiteconnect.KiteConnect`. `market/factory.py` picks which client backs
  it at runtime:
  - If `KITE_API_KEY` is unset (v1 default), it uses `market/mock_kite_client.py` -- an
    offline client returning the same response shapes as real Kite, for a small fixed
    universe of NSE large-caps with deterministic seeded-random-walk prices. No real
    login is needed; `kite_logged_in` is always true in this mode.
  - If `KITE_API_KEY`/`KITE_API_SECRET` are set, it uses the real `kiteconnect.KiteConnect`.
  - Auth (real mode only): `GET /kite/login` redirects to `kite.login_url()`. `GET /kite/callback?request_token=...` calls `generate_session(request_token, api_secret)` and stores the `access_token` in the `kite_session` table along with the date.
  - Tokens expire daily. If there's no valid token, the API returns `409 KITE_LOGIN_REQUIRED` with the login URL, and the UI shows a banner.
  - Instruments dump for NFO and NSE, cached once per day.
  - `history` uses `historical_data(token, from, to, "day")` for the underlying. If the plan doesn't include historical data, raise `NotAvailable` and degrade gracefully.
  - Rate limiting: a token bucket of ≤ 3 requests per second, applied before every `quote()`/`ltp()` call (`market/kite.py::_TokenBucket`).

---

## 9. Guardrails (`rules/guardrails.py`)

This is the single module that decides what is allowed. Every function returns either `Ok` or raises `RuleViolation(code, message, overridable: bool)`.

| Action | Checks |
|---|---|
| `check` (evaluate) | Always allowed. Logged. |
| `record_trade` | **Hard:** monthly realised loss < `monthly_loss_limit`; not in cooling-off; `lots ≤ max_lots` from the linked check; check is < 30 minutes old. **Overridable with reason:** verdict is red. |
| `tighten_stop` | New stop must be more protective: higher premium stop, or a thesis level closer to spot; or an earlier time-stop date. Otherwise reject. |
| `loosen_stop` | **Web UI only** (no API route reachable from MCP; see section 11), requires a reason, and is logged in `overrides`. |
| `update_settings` | Web UI only. A change that loosens any rule gets `effective_from` = next trading day 09:00 IST. A tightening change is effective immediately. |
| `clear_cooloff` | Not available. |

- **Cooling-off** starts when cumulative realised P&L for the calendar month ≤ −`monthly_loss_limit`, and lasts `cooloff_days` trading days.
- **Monthly P&L** is always computed from `trades`, never stored.
- **Route separation:** MCP-callable routes are under `/api/v1/*`. UI-only, loosening routes are under `/ui/*` and require a CSRF token issued to the browser session. The MCP server only knows `/api/v1`.

---

## 10. Database schema (PostgreSQL -- not SQLite, see implementation note at top)

No `alerts` table: the monitor/alerting subsystem is not implemented in v1 (section 12).
Everything else below matches the original design, translated to Postgres (`SERIAL`,
`JSONB`, native `BOOLEAN`). The actual source of truth is `src/optionguard/db/models.py`
plus the Alembic migration in `alembic/versions/`.

```sql
CREATE TABLE settings (
  id                   SERIAL PRIMARY KEY,
  effective_from       TIMESTAMPTZ NOT NULL,
  max_loss_per_trade   NUMERIC NOT NULL,
  monthly_loss_limit   NUMERIC NOT NULL,
  premium_stop_pct     NUMERIC NOT NULL,      -- 0.40
  time_stop_days       INTEGER NOT NULL,      -- trading days before expiry
  cooloff_days         INTEGER NOT NULL,
  show_spreads         BOOLEAN NOT NULL DEFAULT FALSE,
  created_at           TIMESTAMPTZ NOT NULL
);  -- append-only; active row = latest with effective_from <= now

CREATE TABLE corporate_events (
  id          SERIAL PRIMARY KEY,
  symbol      TEXT NOT NULL,
  event_type  TEXT NOT NULL CHECK (event_type IN ('earnings','dividend')),
  event_date  DATE NOT NULL,
  dividend    NUMERIC,
  UNIQUE(symbol, event_type, event_date)
);

CREATE TABLE checks (
  id                 SERIAL PRIMARY KEY,
  created_at         TIMESTAMPTZ NOT NULL,
  batch_id           TEXT NOT NULL,           -- groups candidates checked together
  symbol             TEXT NOT NULL,
  expiry             DATE NOT NULL,
  strike             NUMERIC NOT NULL,
  opt_type           TEXT NOT NULL CHECK (opt_type IN ('CE','PE')),
  data_source        TEXT NOT NULL CHECK (data_source IN ('manual','mock','kite')),  -- a
                                              -- request with source="kite" is tagged "mock"
                                              -- here, permanently, if no real Kite
                                              -- credentials existed at check time -- see
                                              -- implementation note #2 at the top of this doc
  spot               NUMERIC NOT NULL,
  premium            NUMERIC NOT NULL,
  bid NUMERIC, ask NUMERIC,
  lot_size           INTEGER NOT NULL,
  iv NUMERIC, hv20 NUMERIC, hv_percentile NUMERIC,
  breakeven NUMERIC, breakeven_pct NUMERIC, expected_move_pct NUMERIC,
  prob_profit NUMERIC, prob_zero NUMERIC, prob_thesis NUMERIC,
  theta_per_day_lot NUMERIC,
  stretch_5d_pct NUMERIC, stretch_20dma_pct NUMERIC, spread_pct NUMERIC,
  thesis_target NUMERIC, thesis_date DATE,
  verdict            TEXT NOT NULL CHECK (verdict IN ('red','amber','green')),
  flags              JSONB NOT NULL,          -- [{code, level, message}]
  max_lots           INTEGER NOT NULL,
  alternatives       JSONB,
  oi_context         JSONB,
  raw                JSONB NOT NULL,          -- full inputs + outputs
  expiry_settle      NUMERIC,                 -- option value at expiry (backfilled)
  settled_at         TIMESTAMPTZ
);
CREATE INDEX ix_checks_symbol_expiry ON checks(symbol, expiry);

CREATE TABLE trades (
  id                 SERIAL PRIMARY KEY,
  check_id           INTEGER NOT NULL REFERENCES checks(id),
  entry_time         TIMESTAMPTZ NOT NULL,
  lots               INTEGER NOT NULL,
  lot_size           INTEGER NOT NULL,
  entry_premium      NUMERIC NOT NULL,
  stop_premium       NUMERIC NOT NULL,
  thesis_stop_level  NUMERIC,
  time_stop_date     DATE NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('open','closed')),
  exit_time          TIMESTAMPTZ,
  exit_premium       NUMERIC,
  exit_reason        TEXT CHECK (exit_reason IN
                       ('premium_stop','thesis_stop','time_stop','target','manual','expiry')),
  charges            NUMERIC,                 -- default from config estimate, editable
  pnl                NUMERIC                  -- (exit-entry)*lots*lot_size - charges
);

CREATE TABLE stop_changes (
  id          SERIAL PRIMARY KEY,
  trade_id    INTEGER NOT NULL REFERENCES trades(id),
  at          TIMESTAMPTZ NOT NULL,
  field       TEXT NOT NULL,                  -- stop_premium | thesis_stop_level | time_stop_date
  old_value   TEXT, new_value TEXT,
  direction   TEXT NOT NULL CHECK (direction IN ('tighten','loosen'))
);

CREATE TABLE overrides (
  id        SERIAL PRIMARY KEY,
  check_id  INTEGER REFERENCES checks(id),
  trade_id  INTEGER REFERENCES trades(id),
  at        TIMESTAMPTZ NOT NULL,
  rule      TEXT NOT NULL,                    -- RED_VERDICT | LOOSEN_STOP | ...
  reason    TEXT NOT NULL CHECK (length(reason) >= 15)
);

CREATE TABLE kite_session (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  access_token  TEXT,
  valid_for     DATE
);
```

---

## 11. Core API (JSON)

All routes are under `/api/v1` unless they're marked UI-only. Errors use the shape `{error: {code, message, overridable?, login_url?}}`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/checks` | Body: `{candidates: [{text, thesis?, manual?: {...}}], source: "manual"\|"kite"}`. Returns `{batch_id, results: [CheckResult], ranking: [check_id]}` |
| GET | `/checks/{id}` | One check |
| GET | `/positions` | Open trades with live premium, P&L, distance to each stop, and `has_gtt` (true/false for kite/mock trades, `null` for manual -- whether a matching GTT order actually exists in Kite; see implementation note #3) |
| POST | `/trades` | Body: `{check_id, lots, entry_premium, stop_premium?, thesis_stop_level?, time_stop_date?, override_reason?}`. Missing stop fields default from settings. Guardrails apply. |
| POST | `/trades/{id}/tighten` | Body: any of the three stop fields; tighten only |
| POST | `/trades/{id}/exit` | Body: `{exit_premium, exit_reason, charges?}` |
| GET | `/journal/stats` | Query: `from, to, group_by=verdict\|exit_reason\|month\|symbol\|override` |
| GET | `/journal/checks` | Paged list, including untraded checks with `expiry_settle` |
| GET/POST | `/events` | List or add corporate events |
| GET | `/status` | `kite_mode: "mock"\|"live"`, Kite login state, month P&L, loss-limit headroom, cooling-off state |
| GET | `/settings` | Active settings (read-only) |
| — | `/ui/settings` (POST) | **UI-only.** Update settings |
| — | `/ui/trades/{id}/loosen` (POST) | **UI-only.** Requires a reason |
| — | `/kite/login`, `/kite/callback` | Auth flow |

`CheckResult` = every metric in section 7.1, plus `verdict`, `flags`, `max_lots`, `alternatives`, `oi_context`, `explanation_facts` (a list of short factual sentences generated in code, which Claude can quote).

---

## 12. Monitor (`monitor/`) -- **not implemented in v1**

> Removed from the v1 build by request (to be added later if needed). There is no
> `monitor/` package, no APScheduler, no `alerts` table, and no Telegram integration.
> What took its place:
> - **`poll_positions`** → `GET /positions` computes live premium/spot/P&L/distance-to-stop
>   on demand, per request, instead of via a background poller.
> - **`daily_checks`** (time_stop / earnings_tomorrow / expiry_week / kite_login_needed) →
>   not implemented; `trading_days_to_time_stop` and `trading_days_to_expiry` are still
>   returned by `GET /positions` for the UI to display, just not proactively alerted on.
> - **`backfill_settle`** → *is* implemented, as `optionguard backfill-settle` (a CLI
>   command you run manually or cron yourself, per section 4's `cli.py`), not a scheduled
>   in-process job.
>
> The rest of this section describes the original (unbuilt) design, kept for reference
> in case this is revisited later.

APScheduler jobs run in the core process:

| Job | Schedule (IST) | Action |
|---|---|---|
| `poll_positions` | Every 60s, 09:15–15:30 on trading days | Fetch premium and spot for open trades via `kite.ltp`. Fire alerts: `stop_near` (premium within 10% of stop), `stop_hit`, `thesis_break`, `iv_crush` (IV down > 25% since entry) |
| `daily_checks` | 09:00 trading days | `time_stop` (today ≥ time_stop_date), `earnings_tomorrow`, `expiry_week` (≤ 5 trading days), `kite_login_needed` |
| `backfill_settle` | 16:30 trading days | For checks with `expiry < today` and no `expiry_settle`, compute intrinsic value from the underlying's close on expiry day |

- Alerts are de-duplicated: at most one alert per (trade, type) per 30 minutes.
- Delivery goes through `notify.py`, using Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in `.env`). Failed deliveries are retried by the next poll.
- With no Kite session, `poll_positions` is skipped and a `kite_login_needed` alert fires once a day.
- Manual-mode positions get only date-based alerts.

---

## 13. Web UI

Server-rendered with HTMX partials. Mobile-friendly and single-column.

1. **Check** (`/`)
   - Status bar: month P&L / limit, cooling-off state, Kite login state. **A MOCK DATA banner
     shows whenever `kite_mode == "mock"`** -- i.e. no real Kite credentials are configured, so
     every "kite"-sourced number is simulated. Each result card also tags itself `MOCK DATA`
     individually, since a journal entry from before real credentials existed stays tagged
     `data_source = "mock"` forever (see section 10/11).
   - Textarea for candidates (one per line), with a source toggle. In manual mode, inline fields appear per candidate.
   - Result cards: verdict colour, key metrics, flags, max lots, alternatives, OI context.
   - Ranking strip at the top.
   - The **Take trade** button opens a form pre-filled with max lots and default stops. A red verdict requires an override reason.
   - Input is disabled during cooling-off or at the monthly limit.
2. **Positions** (`/positions`)
   - Table of open trades with live P&L and distance to each stop.
   - **Tighten** and **Exit** buttons. **Loosen** is behind a confirm dialog that asks for a reason.
   - ~~Recent alerts.~~ Not present -- no alerting subsystem in v1 (see implementation note
     at top, and section 12).
   - **GTT check:** for each kite/mock position, a read-only call asks Kite whether an active
     GTT order exists for that exact contract (`KiteProvider.has_gtt`, backed by
     `kite.get_gtts()`). If not, the row shows a warning -- the stop in this app is just a
     number until a real order enforces it. This directly addresses principle 6 and the
     "forces exits" goal without a background job or static IP: it's a plain read on page
     load, added after a v1 review flagged that nothing was actually enforcing exits once
     the monitor loop was removed. `has_gtt` is `null`/not-applicable for manual trades.
3. **Journal** (`/journal`)
   - Stats grouped by verdict, exit reason, month and override.
   - A "skipped checks" table showing what untraded candidates were worth at expiry.
4. **Settings** (`/settings`)
   - Risk rules, with a note that loosening applies tomorrow.
   - Corporate events CRUD.
   - Kite login button.
   - Holiday list (read from config, displayed).

---

## 14. MCP server (`mcp_server/server.py`)

Stdio transport, built on `mcp.server.mcpserver.MCPServer` (the `mcp` 2.x API -- `FastMCP`
was renamed; see section 3). Each tool is a thin wrapper over `/api/v1`. Tool descriptions
must tell Claude to report the computed numbers and flags rather than estimate.

| Tool | Maps to | Notes |
|---|---|---|
| `check_options(candidates: list[str], thesis: list[str] \| None, source: "kite"\|"manual" = "kite", manual_inputs: list[dict] \| None)` | POST `/checks` | Returns full results. Check `data_source` -- `"mock"` means every number is simulated; the tool always reports this, never silently |
| `get_positions()` | GET `/positions` | Check `has_gtt` per position -- `false` means the stop isn't actually enforced by a real order |
| `get_status()` | GET `/status` | Check `kite_mode` -- `"mock"` means nothing here has touched the real market |
| `record_trade(check_id, lots, entry_premium, stop_premium?, thesis_stop_level?, time_stop_date?, override_reason?)` | POST `/trades` | Surface `RuleViolation` messages verbatim |
| `tighten_stop(trade_id, ...)` | POST `/trades/{id}/tighten` | |
| `exit_trade(trade_id, exit_premium, exit_reason)` | POST `/trades/{id}/exit` | |
| `journal_stats(from_date, to_date, group_by)` | GET `/journal/stats` | params renamed from `from`/`to` -- reserved word in Python |
| `list_checks(from_date, to_date, verdict?, traded?)` | GET `/journal/checks` | same rename |
| `add_corporate_event(symbol, event_type, event_date, dividend?)` | POST `/events` | |

Intentionally absent: settings updates, loosening stops, clearing cooling-off, and order placement.

Claude Desktop config example (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "optionguard": {
      "command": "uv",
      "args": ["run", "--directory", "/ABSOLUTE/PATH/optionguard", "python", "-m", "mcp_server.server"],
      "env": { "OPTIONGUARD_URL": "http://127.0.0.1:8787" }
    }
  }
}
```

---

## 15. Configuration

`.env.example` (as actually shipped -- no Telegram vars, since alerting isn't implemented;
also no real DB credentials, since this file is committed to a public repo)
```
DATABASE_URL=postgresql+psycopg://youruser:yourpassword@127.0.0.1:5432/optionguard
KITE_API_KEY=
KITE_API_SECRET=
KITE_REDIRECT_URL=http://127.0.0.1:8787/kite/callback
OPTIONGUARD_SECRET_KEY=
```

`config.toml`
```toml
[server]
port = 8787

[pricing]
risk_free_rate = 0.0    # SET to current 91-day T-bill yield, e.g. 0.06
trading_days_per_year = 252

[defaults]                # seeds the first settings row
max_loss_per_trade = 5000
monthly_loss_limit = 10000
premium_stop_pct = 0.40
time_stop_days = 7
cooloff_days = 10
charges_per_order_estimate = 60

[verdict]
low_prob_red = 0.25
low_prob_amber = 0.35
be_to_move_red = 0.8
chasing_5d_pct = 0.08
chasing_20dma_pct = 0.07
iv_hv_amber = 1.3
hv_pct_amber = 80
spread_red = 0.10
spread_amber = 0.05
min_volume = 50
min_oi = 200
near_expiry_days = 8

[holidays]
dates = []   # "YYYY-MM-DD" NSE trading holidays, user-maintained
```

---

## 16. Testing

- **pricing:**
  - BS prices against textbook values (e.g. Hull's examples, to within 1e-4).
  - Put-call parity.
  - IV round-trip (price → IV → price) across moneyness and T.
  - `implied_vol` returns `None` below intrinsic.
  - Probabilities lie within [0, 1] and are monotone in the strike.
- **sizing:** edge cases (zero lots, gap protection binding).
- **verdict:** each rule triggers at its threshold; red dominates amber.
- **guardrails:**
  - Monthly limit and cooling-off computed from fixture trades.
  - Tighten accepts only protective changes.
  - A red verdict without a reason is rejected; with a reason it's accepted and logged.
  - Lots over `max_lots` are rejected (not overridable).
  - A stale check (> 30 minutes) is rejected.
  - Settings loosening is delayed to the next trading day.
- **API:** `/ui/*` routes reject requests without a CSRF token.
- **data tagging:** a `source="kite"` check with no real credentials is stored and returned
  as `data_source="mock"`, never `"kite"`; `/status` reports `kite_mode` accordingly
  (`tests/test_api.py::test_mock_backed_kite_check_is_tagged_mock_not_kite`). Added after a
  design review caught the original build conflating the two.
- **GTT check:** `has_gtt` is true/false for a matching/missing active GTT order
  (`tests/test_kite_provider.py`), and `null` for manual positions where it's not
  applicable (`tests/test_api.py::test_positions_surfaces_has_gtt_for_mock_none_for_manual`).
- **KiteProvider:** test against the offline `MockKiteClient` fixture (deterministic, seeded
  random-walk prices -- stands in for a "recorded-fixture fake" since it's fully offline).
  No live calls in tests.
- **MCP:** each tool maps to the right route; a core-down error is readable.

---

## 17. Milestones and acceptance criteria

**M1: Pricing + manual check (CLI/API).** `pricing/`, `analysis/` and the manual provider, plus `POST /checks`, the DB, and logging of checks. The tests in section 16 for pricing, sizing and verdict pass.
✅ Posting `TCS NOV 2500 CE` with manual spot/premium/lot/expiry returns the full metrics and a verdict, and stores a row in `checks`.

**M2: MCP in Claude Desktop.** The `mcp_server` with `check_options`, `get_status` and `add_corporate_event`.
✅ In Claude Desktop, "check TCS NOV 2500 CE, spot 2400, premium 30, lot 175" returns the card data, and Claude explains it from the returned numbers.

**M3: Trades + guardrails.** Trades, stop changes and overrides, with all the guardrail tests passing, plus the corresponding MCP tools.
✅ An oversized trade is rejected; a red-verdict trade requires a reason; a tighten that loosens is rejected.

**M4: Kite provider.** Auth flow, instruments cache, quotes, history, chain, OI context and alternatives.
✅ A Kite-mode check needs only the candidate text, and degrades cleanly if historical data isn't available. Built against an offline mock client by default; the real `kiteconnect.KiteConnect` is a credential-only swap (section 8).

**M5: Monitor + Telegram.** ❌ **Not built in v1** -- removed by request; see section 12. Live P&L on the Positions screen is computed on demand instead of via a scheduled poller.

**M6: Web UI.** The four screens; settings loosening is delayed to the next day.
✅ The whole flow (check → take trade → exit → journal) works in the browser. ("Monitor" here means viewing the Positions screen, not a background job -- see M5.)

**M7: Journal stats + settle backfill.**
✅ Stats grouped by verdict show trade counts, win rate and average P&L; skipped checks show their value at expiry.

---

## 18. Out of scope / explicit non-goals
- Any directional prediction or "buy" recommendation. Verdicts describe risk and odds only.
- Option selling, except as the covered short leg of a debit spread in v2.
- Multi-user support, cloud hosting, or any exposure beyond 127.0.0.1.
