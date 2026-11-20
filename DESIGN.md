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
> 5. **Docker Compose deployment and an HTTP transport for the MCP server are new
>    capabilities not in the original design at all** (the original only specified stdio
>    MCP and a bare `uv run optionguard serve`). See section 4's "Docker deployment"
>    subsection and section 14.
>
> Also added since the original design: section 7.6 (holding-period metrics -- delta,
> theta-offset, a short-horizon P&L scenario grid), fully implemented, not just specified.
>
> 6. **Section 7.7, the stock-price thesis journal, was built for an earlier version of
>    this app and has since been removed** (code, DB tables, docs) -- superseded by
>    section 7.9's option theses, which tests a shorter-horizon view on the option
>    contract's own premium instead. `theses`, `thesis_drivers`, `thesis_option_bars` and
>    `thesis_scores` were dropped by migration; `sync_runs` (created in the same original
>    migration but genuinely shared sync-pipeline infrastructure) was kept, and its
>    advisory-lock sync job now lives at `optionguard/sync.py` rather than inside a
>    `theses/` package. If you're reading an old copy of this doc or an old PR that still
>    describes 7.7, treat it as historical -- it is not what's running.
>
> 7. **Section 7.8, the order ticket, is also new since the original design, and changes
>    principle 6** (it's now `## 1`'s item 6 below -- v1 *can* place orders, from the web UI
>    only). Built and testable end to end against the mock Kite client -- a real static IP
>    (required by Kite for order/GTT endpoints since 1 April 2026) hasn't been set up yet,
>    so live order placement itself is unverified; everything up to that boundary (review,
>    confirm, fill polling, GTT math, exit, reconciliation) is. Known gaps against 7.8's
>    description:
>    - **No time-of-day/market-hours gating.** The ticket doesn't disable itself outside
>      `09:15`–`entry_cutoff` for entries or outside market hours for exits -- it currently
>      works at any hour as long as the other guardrails pass.
>    - **The `egress_proxy` config field exists but isn't wired up.** 7.8.2's "route order
>      calls only through a small VPS with a static IP" option -- a dedicated, differently-
>      routed order client via `KiteConnect(..., proxies=...)` -- isn't built. This is the
>      option most relevant if a home ISP won't give a static IP.
>    - **Reconciliation's "untracked Kite position" amber notice is not implemented** -- no
>      cross-check against Kite's own `positions()`/`holdings()` for contracts the app
>      doesn't know about.
>    - **Tightening a GTT-backed trade rebuilds it as a single stop leg**, dropping any
>      original OCO target -- `trades` has no `target_premium` column to remember it by
>      (only `order_intents` does, transiently, per 7.8.7's own schema).
>    - **Order/GTT calls aren't rate-limited.** The existing `_TokenBucket` (3/sec) still
>      only covers `quote()`/`ltp()`/`chain()`; 7.8.2's 10/sec order limit isn't enforced
>      separately, since nothing here can place enough orders to approach it yet.
>    - **`market_protection` isn't passed to `place_order`.** Harmless while
>      `allow_market = false` (the default), but needed before ever turning it on for real,
>      per 7.8.2's warning that Kite rejects zero-market-protection MARKET orders.
>
> 8. **Section 7.9, option theses, is also new since the original design.** Implemented
>    close to its full spec -- config, the Monte Carlo and implied-spot pricing, the mock
>    client's minute bars, the DB tables + settable-once-from-null immutability trigger, the
>    pure resolution/scoring/exit-policy modules, the bars → resolve → score → policies sync
>    step (folded into the shared `optionguard/sync.py` login/button/CLI job), the scorecard with its policy
>    comparison block, the order-ticket `thesis_id` tighten-only integration, MCP tools and
>    the Option theses web screen. One known gap against 7.9.10's description:
>    - **The path chart has no underlying overlay.** `option_thesis_scores.path` carries only
>      the option's own downsampled (15-minute) closes, not the underlying's -- there's no
>      underlying minute-bar table in this build (only the option's), so the spec's "option
>      close + underlying on a second axis" chart can't show the spot overlay yet. The chart
>      still renders the option's own path with stop/entry/target lines and the exit marked.
>
> Sections 1, 4, 7.3, 9, 10, 11, 13, 14 and 15–17 below are annotated inline where they
> differ from what's running.

---

## 1. Principles (non-negotiable)

1. **All rules are enforced in the core service**, never in the UI or the LLM. Any caller (web UI, MCP, script) hits the same checks.
2. **All numbers are computed in code.** The MCP layer returns structured data; Claude only explains it.
3. **Rules can be tightened anywhere, loosened only in the web UI**, and loosened settings take effect the **next trading day**. GTT stops follow the same rule (section 7.8.5): tighten moves the real GTT from anywhere tighten is reachable, loosen only from the web UI.
4. **Every candidate is logged**, traded or not, so outcomes can be compared later.
5. **Every override needs a typed reason**, stored in `overrides`.
6. **Orders are placed only from the web UI, one at a time, after an explicit confirm, and only through the guardrails** (section 7.8, changed from the original "v1 places no orders"). Every filled entry gets a GTT stop. MCP and `/api/v1` never place, modify or cancel an order or a GTT.

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
    cli.py                 # `optionguard serve`, `optionguard sync`, `optionguard backfill-settle`
                            # (sync is now 4 steps -- reconcile orders/GTTs (7.8.6), bars, score,
                            # settle; backfill-settle is still an alias for just the settle step)
    config.py              # loads config.toml + .env (pydantic-settings)
    app.py                 # FastAPI app factory (binds 127.0.0.1 only -- no scheduler in v1)
    db/{models.py, repo.py, session.py}
    pricing/{bs.py, iv.py, probability.py, calendar.py}
    market/{base.py, manual.py, kite.py, mock_kite_client.py, factory.py, symbols.py}
    analysis/{metrics.py, verdict.py, sizing.py, alternatives.py, oi.py, explain.py}
    rules/{guardrails.py, errors.py}
    journal/stats.py
    sync.py                 # shared sync job: reconcile orders/GTTs, settle checks, option theses
    orders/{gtt.py, service.py}  # section 7.8, added -- review/confirm/poll/GTT/exit/reconcile
    option_theses/{derived.py, resolve.py, scoring.py, policies.py, scorecard.py, sync.py}  # section 7.9, added
    api/{checks.py, trades.py, settings.py, events.py, journal.py, kite_auth.py, status.py,
         option_theses.py, orders.py, schemas.py, serializers.py, errors.py}   # option_theses.py is
                                                                          # 7.9, orders.py is 7.8; the
                                                                          # last three weren't in the
                                                                          # original list
    web/{routes.py, csrf.py, templates/, static/}
  mcp_server/
    server.py              # MCPServer tools, shared by both entrypoints below (mcp 2.x; see section 3)
    http_main.py           # Streamable HTTP + bearer auth -- added, not in original design; see section 14
  Dockerfile               # added, not in original design; see "Docker deployment" below
  docker-compose.yml       # added -- `app` + `mcp` services
  .dockerignore            # added
  tests/
```

### Docker deployment (added, not in original design)

The original design only specified `uv run optionguard serve` and a stdio MCP server.
`Dockerfile` + `docker-compose.yml` are a later addition, for running both as containers.

- **One image, two entrypoints.** The same `Dockerfile` build is used by both
  `docker-compose.yml` services; `command:` picks which one runs:
  - `app` service: default `CMD` (`alembic upgrade head && exec optionguard serve`),
    published on `127.0.0.1:8787`.
  - `mcp` service: `command: ["python", "-m", "mcp_server.http_main"]`, published on
    `127.0.0.1:4101`. Gets a deliberately minimal environment -- only `OPTIONGUARD_URL`
    (set to `http://app:8787`, the `app` service's name on the Docker network) and
    `MCP_AUTH_TOKEN` -- no DB credentials or Kite secrets, since per section 4 the MCP
    server never needs them.
- **`OPTIONGUARD_HOST` env var** (read in `cli.py`): binding `127.0.0.1` -- correct for a
  bare `uv run optionguard serve` on your own machine -- would make the app unreachable
  from *outside* a container entirely, since Docker's port publishing reaches the
  container's own interface, not its loopback. The Dockerfile sets
  `OPTIONGUARD_HOST=0.0.0.0` so uvicorn binds that inside the container instead; the
  "never exposed beyond 127.0.0.1" guarantee is preserved from the host's point of view by
  `docker-compose.yml` publishing each port as `127.0.0.1:<port>:<port>`, not `<port>:<port>`.
- **`DATABASE_URL_DOCKER`** (`.env`, section 15): the `app` container reaches the `pgdb`
  Postgres container by its container name on the shared `pwm-net` Docker network, not
  `127.0.0.1` (which inside a container means the container itself). This overrides
  whatever plain `DATABASE_URL` is set to (that one's for running `uv run optionguard
  serve` directly on the host).
- **`pwm-net`**: an external Docker network shared with other local apps (e.g. `pwm`) so
  containers across different `docker compose` projects can reach each other's named
  services -- not created by this repo's `docker-compose.yml`, see its header comment for
  one-time setup.
- `optionguard backfill-settle` still works against a running `app` container:
  `docker compose exec app optionguard backfill-settle`.

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
| `delta`, `daily_move_needed`, `daily_move_needed_pct`, `scenarios` | Section 7.6 (added) |

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
| `DELTA_TOO_SMALL` | `abs(delta) < 0.05` (section 7.6, added) | amber |

### 7.3 Sizing (`sizing.py`)

```
loss_per_lot_at_stop = premium × lot_size × premium_stop_pct
full_loss_per_lot    = premium × lot_size
max_lots = min(
  floor(max_loss_per_trade / loss_per_lot_at_stop),
  floor(2 × max_loss_per_trade / full_loss_per_lot)   # protects against gaps through the stop
)
```

Section 7.8.5's GTT limit buffer (sell limit = trigger × (1 − `gtt_limit_buffer_pct`), trading a worse fill for a better chance of actually exiting on a gap) is the same gap this 2× rule already assumes can happen -- not a new risk, just a second place it shows up.

Section 7.9's option theses reuse `max_loss_per_trade` directly (`loss_vs_cap = risk_per_lot × lots / max_loss_per_trade`) rather than this `max_lots` formula -- a warning above 1.0 for a paper thesis, a hard 422 for a real one.

### 7.4 Alternatives (`alternatives.py`)

> **Revised post-launch** (the original rule below produced an empty list whenever the
> requested strike was already the nearest or 2nd-nearest listed strike to spot -- the
> common case for a reasonably-chosen candidate, not an edge case, since "strikes between
> the requested strike and spot" is necessarily empty once there's no room left in that
> gap). Now: the nearest **6 listed strikes to the requested strike itself**, either side,
> excluding the requested strike. Also no longer excludes `verdict == red` candidates --
> a flag like `NEAR_EXPIRY` applies identically to every strike on the same expiry, so the
> old red-exclusion could silently return nothing even when a strike right next door had a
> materially better `prob_profit`. `max_lots ≥ 1` is still required (a zero-lot alternative
> isn't actionable regardless of its colour).

- Kite mode only. Consider the same expiry and type, up to 6 listed strikes nearest the requested one (either side).
- Compute the full metrics for each and return the **top 2** by `prob_profit` among those with `max_lots ≥ 1`, regardless of verdict colour.
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

---

### 7.8 Order ticket (added, implemented against the mock client)

> Changes principle 6 (`## 1`); see the top-of-doc implementation note #7 for the six known
> gaps (no time-of-day gating, `egress_proxy` not wired up, no untracked-position notice,
> tighten drops an OCO target on rebuild, no order-specific rate limit, no
> `market_protection`) before relying on this section as a description of exactly what's
> running. Everything else here works end to end against the mock client; live order
> placement against the real Kite API is unverified -- there's no whitelisted static IP yet.

**Purpose:** place entry and exit orders for a checked contract from the local web UI. The screen is laid out like Kite's order window. Every order passes the section 9 guardrails *before* it reaches Kite. Every filled entry gets a GTT stop placed automatically, so a position never sits without a broker-side stop. Section 7.9.10 extends this same ticket with an optional `thesis_id`: pre-filled, tighten-only lots/stop/target, and linking the fill back to the option thesis.

**Non-goals:**
- No order placement from MCP, the CLI or any `/api/v1` route. Claude can never place, modify or cancel an order.
- No selling to open. SELL exists only to close an open trade the app knows about.
- No intraday products (MIS), no bracket or cover orders, no basket orders.
- No automated or scheduled orders. A human clicks Confirm for every order.

---

#### 7.8.1 Principle change

Principle 6 becomes (and is now worded this way in `## 1` above):

> **Orders are placed only from the web UI, one at a time, after an explicit confirm, and only through the guardrails.** Every filled entry gets a GTT stop. MCP and `/api/v1` never place, modify or cancel orders.

Principle 3 (loosen only in the web UI) now also covers GTT stops: loosening a GTT goes through the existing `/ui/trades/{id}/loosen` route. Tightening -- reachable from both `/api/v1` and the web UI -- also moves the real GTT (`orders/service.py`'s `sync_gtt_after_stop_change`), since it can only make things more protective.

---

#### 7.8.2 Preconditions and config

```toml
[orders]
enabled              = false     # master switch; the ticket is read-only (preview only) when false
allow_market         = false     # LIMIT only unless explicitly enabled
market_protection    = 3         # percent; used only if allow_market = true. Must be non-zero
gtt_limit_buffer_pct = 0.10      # GTT sell limit = trigger × (1 − buffer); see 7.8.5
fill_poll_seconds    = 2
fill_poll_timeout_s  = 120       # stop polling after this; order stays tracked, see 7.8.4
entry_cutoff         = "15:15"   # IST; no new entries after this -- NOT currently enforced, see top note
egress_proxy         = ""        # optional, e.g. "http://10.8.0.1:3128" -- NOT currently wired up, see top note
```

**Regulatory constraints (verify against current Zerodha docs before go-live):**
- **Static IP.** Since 1 April 2026, Kite rejects order endpoints (place, modify, cancel) from IPs that aren't whitelisted on the developer profile. Quotes, positions, order book and GTT reads work from any IP. GTT create/modify/delete is assumed to count as an order endpoint.
- **Market protection.** Market orders, including SL-M, are rejected when market protection is 0. The default here is LIMIT-only, which avoids the issue. `market_protection` is configured but not yet passed to `place_order` -- see the top-of-doc note.
- **Rate limit.** 10 orders per second. The existing token bucket from section 8 covers quotes/chain only; order/GTT calls aren't separately throttled yet (see the top note) -- harmless at this volume.

**Static IP options** (home broadband is usually dynamic):
1. A static IP from the ISP, whitelisted directly.
2. **Order calls only, routed through a small VPS with a static IP.** Pass `proxies` to `KiteConnect` for a dedicated *order client*; data calls keep using the normal client. Set this with `egress_proxy`. Only the VPS IP is whitelisted. Exchange rules allow one IP change per calendar week, so pick one and keep it. *Not wired up yet -- `egress_proxy` exists in config but `KiteProvider` doesn't construct a second, proxied client.*

**Status:** `GET /status` gains `orders: {enabled, ip_ok, last_ip_error}`. `ip_ok` is `null` until the first real order attempt (`db/repo.py`'s `get_latest_order_attempt`), then `false` only if that attempt's rejection looks like Kite's static-IP error (`orders/service.py`'s `is_ip_error_message` -- a loose heuristic on the message text, not an exact string match). The status bar shows a red banner when `ip_ok == false`.

**Plan tier:** live quotes on the ticket need the Connect (paid) plan. That plan is already needed for history and theses.

---

#### 7.8.3 The ticket screen (`/order`)

Opened from a check card's **Buy** button (replacing the old **Take trade** button, which moved behind **Record a trade placed elsewhere**), or from a Positions row's **Exit via ticket** button. It is never opened blank: an entry ticket requires `?check_id=`, an exit ticket requires `?trade_id=`.

**Layout** (modelled on Kite's order window, without Kite branding):

| Area | Entry ticket | Exit ticket |
|---|---|---|
| Header | Contract, exchange `NFO`, a BUY tag (blue/green) | Contract, a SELL tag (red) |
| Live strip | LTP, bid, ask, spread %, refreshed every 3s via HTMX polling | Same |
| Quantity | **In lots**, with lot size and quantity shown beside it. Capped at the check's `max_lots`; the field cannot go higher | Defaults to the open quantity; can be lower (a partial exit) |
| Price | LIMIT, pre-filled with the ask (entry) or bid (exit), tick-rounded. MARKET only if `allow_market` | Same |
| Product | NRML, fixed (GTT does not support MIS) | NRML, fixed |
| Validity | DAY, fixed | DAY, fixed |
| Stop | Premium stop, defaulting from settings (`premium_stop_pct`); tighter is allowed. GTT preview: trigger, limit and quantity | — |
| Target | Optional. If set, the GTT is OCO (stop + target) instead of single | — |
| Risk box | Cost (price × qty), loss at stop, loss if the stop gaps by 2×, target premium if set | Estimated proceeds at this price |
| Override | Required text box (≥ 15 chars) if the verdict is red | — |

**Two-step submit:**
1. **Review** (`POST /order/review-html`, no CSRF) runs every guardrail (7.8.4) server-side and renders the risk box. This is where rejections appear.
2. **Confirm** (`POST /order/confirm-html`, CSRF) places the order. The button is disabled after one click (`hx-disabled-elt`), and the request carries the `intent_id` from step 1, so a double-submit is a no-op (7.8.7).

*Not currently implemented: the ticket disabling itself outside `09:15`–`entry_cutoff` IST for entries, outside market hours for exits, when Kite isn't logged in, or during cooling-off. It works at any hour as long as the guardrails in 7.8.4 pass -- see the top-of-doc note.*

---

#### 7.8.4 Entry flow

1. **Review** (`orders/service.py`'s `review_entry`):
   - Runs the existing `record_trade` guardrails **before** any order: monthly limit, cooling-off, `lots ≤ max_lots`, check < 30 min old, red verdict needs a reason.
   - Adds new checks (`rules/guardrails.py`):
     - `guard_order_price`: the limit price is within ±5% of the live LTP (fat-finger guard).
     - `guard_no_live_order_intent` / a trade check: no open trade or open order intent exists on the same contract.
     - `guard_stop_no_looser_than_settings`: the stop is no looser than settings.
   - Creates an `order_intents` row with `status = 'reviewed'`. An intent expires 2 minutes after review (`guard_intent_not_expired`).
2. **Confirm** (`confirm_entry`, CSRF via `/ui/order/confirm`):
   - Re-runs the guardrails, because state may have changed.
   - The `reviewed → placed` transition is a conditional `UPDATE … WHERE status = 'reviewed'` (`repo.try_transition_order_intent`) -- a double-submit of the same `intent_id` just returns the existing result, never a second order.
   - Calls `KiteProvider.place_order(symbol, expiry, strike, opt_type, "BUY", quantity, order_type, price, tag=f"OG{intent_id}")`.
   - Stores `order_id`; intent `status = 'placed'`.
   - A Kite rejection (including the static-IP error) sets `status = 'rejected'`, stores Kite's message in `kite_message`, and shows it verbatim -- caught as a plain `Exception`, not narrowed to a specific `kiteconnect` exception type, so it also catches whatever `NotAvailable` the read-path wrapping (section 8) might raise if even *resolving* the contract fails.
3. **Track the fill:** there is no server-side polling loop. The ticket's status partial (`_order_status.html`) polls `GET /order/{intent_id}/status-html` (which calls `poll_entry_fill`) every `fill_poll_seconds` via HTMX, for as long as the intent is still `placed` -- the same "compute on demand per request" pattern `GET /positions` already uses, not a background job.
   - `COMPLETE`: go to step 4 with `average_price` and `filled_quantity`.
   - `REJECTED` / `CANCELLED` with 0 filled: intent closed, nothing recorded.
   - **Partial fill, then cancelled:** treat the filled quantity as the trade (`status = 'partial'`).
   - Still `OPEN` after `fill_poll_timeout_s`: the ticket shows "Order open at Kite" with **Modify price** and **Cancel** buttons. The intent stays `placed`. The next page load or sync resumes tracking (7.8.6).
4. **Record and protect** (on any fill > 0):
   - Insert the `trades` row: `entry_premium = average_price`, `lots = filled_qty / lot_size`, and `stop_premium = average_price × (1 − stop_pct)` using the **actual fill price**, not the limit price -- `stop_pct` is derived from `(submitted_price − submitted_stop) / submitted_price` at review time, then reapplied to the fill price, so a limit that doesn't fill at its own price still gets a correctly-scaled stop.
   - Place the GTT (7.8.5) and store `gtt_id` on the trade.
   - If GTT placement fails, the trade still records, but `gtt_status = 'missing'`. The same red banner `has_gtt` already drives (Positions) covers this. A **Retry GTT** button calls `orders/service.py`'s `retry_gtt`.

---

#### 7.8.5 GTT stop

- **Single trigger** (no target): `place_gtt(trigger_type="single", tradingsymbol, exchange="NFO", trigger_values=[stop], last_price=LTP, orders=[{transaction_type: "SELL", quantity, order_type: "LIMIT", product: "NRML", price: limit}])`.
- **OCO** (target set): `trigger_values=[stop, target]` (ascending, index-aligned with `orders`) and two SELL legs -- `orders/gtt.py`'s `build_gtt`, a pure function.
- **Limit price:** `limit = round_to_tick(trigger × (1 − gtt_limit_buffer_pct))` for the stop leg; the target leg sells at the target itself, no discount. On thin far-OTM strikes, a limit at the trigger may not fill after a gap. The buffer trades a worse fill for a better chance of getting out. The sizing rule's 2× gap protection (7.3) already assumes this can happen.
- **Known behaviour (surfaced via `gtt_status`, not yet as ticket help text):**
  - A GTT fires even when the price gaps past the trigger.
  - It fires only once. If the triggered order doesn't fill, there is **no stop** until a new GTT is placed (7.8.6 detects this -- `gtt_status = 'triggered'`).
  - F&O contract GTTs trigger only until 15:40 IST.
- **Tighten** (`POST /trades/{id}/tighten`, existing route) also calls `modify_gtt` via `sync_gtt_after_stop_change`. This is the one Kite-side effect allowed from `/api/v1`, because it can only reduce risk. **The rebuild is single-leg only** -- any original OCO target is dropped, since `trades` has no `target_premium` column (only `order_intents` does, transiently). MCP's `tighten_stop` therefore moves the real GTT too.
- **Loosen** (`/ui/trades/{id}/loosen`, existing) also calls `sync_gtt_after_stop_change`, with a reason logged in `overrides`.
- **Trailing:** out of scope for 7.8.

---

#### 7.8.6 Exit flow and reconciliation

**Manual exit (exit ticket):**
1. The order is a SELL LIMIT for the open quantity (or less), placed through the same review/confirm/poll path (`review_exit`/`confirm_exit`/`poll_exit_fill`).
2. On a full fill (`record_exit_fill`):
   - **Delete the GTT first**, then record the exit with `exit_reason = 'manual'` (or whatever tag the ticket's optional reason field carried).
   - If the GTT delete fails, `gtt_status = 'missing'` on the now-closed trade -- recorded, not raised; there's no persistent UI surface for a closed trade's GTT state today, but the data is there.
3. On a partial exit: modify the GTT quantity down to the remaining quantity instead of deleting it.

**Reconciliation** (`orders/service.py`'s `reconcile`) runs as **step 0 of sync** (`optionguard/sync.py`'s `run_sync`: reconcile, settle checks, then section 7.9's option theses pipeline):
- For each open trade with an `active` `gtt_id`, call `get_gtt(gtt_id)`:
  - **`triggered`, and the resulting leg is `COMPLETE`:** record the exit at that leg's average price, with `exit_reason = 'premium_stop'` or `'target'` depending on which leg (`build_gtt`'s target leg is always last) -- skipping the GTT delete/modify dance `record_exit_fill` would otherwise do, since the GTT already fired and consumed itself.
  - **`triggered`, but the leg isn't filled:** `gtt_status = 'triggered'` (the position is unprotected -- a new GTT must be placed). *Not yet implemented: a dedicated "re-place GTT" button or exit-ticket pre-fill for this specific state; the general exit ticket and `retry_gtt` both still work.*
  - **`cancelled` / `deleted` / `expired` outside the app:** `gtt_status = 'missing'`.
- For each `order_intents` row still `placed` (entry or exit): resume polling via `poll_entry_fill`/`poll_exit_fill`.
- **Positions at Kite with no matching app trade** (an order placed in the Kite app directly): *not implemented* -- see the top-of-doc note. No cross-check against Kite's own position list exists yet.

Reconciliation works without a static IP (`get_gtt`/`order_history` are read endpoints); only the corrective actions (re-place, delete, modify) need one.

---

#### 7.8.7 Database

```sql
CREATE TABLE order_intents (
  id              SERIAL PRIMARY KEY,
  created_at      TIMESTAMPTZ NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('entry','exit')),
  check_id        INTEGER REFERENCES checks(id),
  trade_id        INTEGER REFERENCES trades(id),
  tradingsymbol   TEXT NOT NULL,  -- app-internal "SYMBOL|expiry|strike|opt_type" dedup key,
                                  -- not Kite's own tradingsymbol string (KiteProvider still
                                  -- resolves that itself from symbol/expiry/strike/opt_type)
  quantity        INTEGER NOT NULL,
  order_type      TEXT NOT NULL CHECK (order_type IN ('LIMIT','MARKET')),
  price           NUMERIC,
  stop_premium    NUMERIC,
  target_premium  NUMERIC,
  override_reason TEXT,
  status          TEXT NOT NULL CHECK (status IN
                    ('reviewed','placed','filled','partial','rejected','cancelled','expired')),
  order_id        TEXT,
  filled_qty      INTEGER,
  average_price   NUMERIC,
  kite_message    TEXT,
  raw             JSONB,                       -- unused today; reserved for request + order_history dumps
  CHECK ((kind = 'entry' AND check_id IS NOT NULL) OR (kind = 'exit' AND trade_id IS NOT NULL))
);
CREATE UNIQUE INDEX ux_order_intents_live_contract
  ON order_intents(tradingsymbol) WHERE status IN ('reviewed','placed');

ALTER TABLE trades
  ADD COLUMN entry_intent_id INTEGER REFERENCES order_intents(id),  -- added via use_alter (circular FK)
  ADD COLUMN exit_intent_id  INTEGER REFERENCES order_intents(id),  -- added via use_alter (circular FK)
  ADD COLUMN gtt_id          BIGINT,
  ADD COLUMN gtt_status      TEXT CHECK (gtt_status IN ('active','missing','triggered','deleted'));

-- Section 7.9, added later: order_intents.option_thesis_id -> option_theses(id), one-way
-- (no use_alter needed). See 7.9.10's order-ticket integration and section 10.
```

- **Idempotency:** Confirm is accepted only for an intent in `reviewed` status; the transition to `placed` is a conditional `UPDATE … WHERE status = 'reviewed'`. The partial unique index blocks two live intents on one contract.
- Manually recorded trades (no intent, via "Record a trade placed elsewhere") remain allowed; `gtt_id`/`gtt_status` stay `NULL` until a **Retry GTT** click places one.
- `order_intents.trade_id → trades.id` and `trades.entry_intent_id/exit_intent_id → order_intents.id` are a genuine circular FK pair; the migration creates `order_intents` first (referencing the already-existing `trades`), then `ALTER TABLE trades ADD COLUMN ... REFERENCES order_intents(id)` afterward. The SQLAlchemy models mark those two columns `use_alter=True` so `create_all()` (used by the test suite) handles the same cycle without needing Alembic.

---

#### 7.8.8 API, MCP, UI

**UI-only, CSRF-protected (`/ui/*` -- never reachable from `/api/v1` or MCP):**

| Method | Path | Purpose |
|---|---|---|
| POST | `/ui/order/review` | 7.8.4 step 1 |
| POST | `/ui/order/confirm` | 7.8.4 step 2 |
| GET | `/ui/order/{intent_id}/status` | Poll the fill state (7.8.4 step 3) |
| POST | `/ui/order/{intent_id}/modify` | Modify the price of an open order |
| POST | `/ui/order/{intent_id}/cancel` | Cancel an open order |
| POST | `/ui/trades/{id}/gtt` | Retry/re-place the GTT |

**Read-only, `/api/v1` (MCP may call this one):**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/orders/intents` | List `order_intents`, filterable by `status` |

**Server-rendered pages (`web/routes.py`, not JSON):** `GET /order?check_id=…` or `?trade_id=…` (the ticket itself), plus `-html` HTMX-partial routes (`/order/quote-html`, `/order/review-html`, `/order/confirm-html`, `/order/{id}/status-html`, `/order/{id}/modify-html`, `/order/{id}/cancel-html`, `/trades/{id}/gtt-html`) that call straight into the `/ui`/`/api/v1` handler functions, the same pattern section 7.9's Option theses screen uses.

- **MCP:** no new action tools. `get_positions` gains `gtt_status` in its per-position output, and `list_order_intents` is a new read-only tool. Both tool descriptions state orders can only be placed, modified or cancelled from the web UI.
- **UI:**
  - The check card's **Take trade** became **Buy**, which opens the ticket. The old record-only form moved behind **Record a trade placed elsewhere**.
  - Positions' rows gained **Exit via ticket**; the old manual exit form moved behind **Record an exit placed elsewhere**. A red banner plus **Retry GTT** button appears when `gtt_status == 'missing'`; a different banner with a direct exit-ticket link appears when `gtt_status == 'triggered'` (fired but unfilled).
  - The status bar shows `orders: enabled/preview only` and a red banner when `ip_ok == false`.
- **Mock mode:** `MockKiteClient` gained `place_order`, `order_history`, `modify_order`, `cancel_order`, `place_gtt`, `get_gtt`, `modify_gtt` and `delete_gtt`, with deterministic fills: a LIMIT at or through the mock's current touch price (ask for BUY, bid for SELL) fills immediately at that price; otherwise it stays `OPEN`. GTT triggering is evaluated lazily on `get_gtt()`/`get_gtts()`, the same way `spot()` is lazily derived from the price path -- no background job needed to decide a GTT has fired. This lets the whole flow (reviewed through to reconciliation) be tested offline, which is how section 7.8.9's suite runs today, since there's no whitelisted static IP yet.

---

#### 7.8.9 Tests

- **Guardrails before placement:**
  - Over `max_lots`, cooling-off, monthly limit, a stale check, or a red verdict without a reason → no `place_order` call at all (assert on the mock).
  - A price beyond ±5% of LTP → rejected.
- **Idempotency:** a double Confirm places one order; a second live intent on the same contract → 409.
- **Fill handling:**
  - A full fill records the trade at `average_price` and places a GTT for the full quantity.
  - A partial fill then cancel records the filled lots only, with a GTT for that quantity.
  - A rejection records nothing.
  - A timeout leaves the intent `placed` and resumes on reload.
- **GTT:**
  - The trigger uses the fill price, not the limit.
  - The limit applies the buffer and is tick-rounded.
  - OCO when a target is set.
  - GTT failure → `gtt_status = 'missing'`.
- **Exit:**
  - The GTT is deleted before the exit is recorded.
  - A partial exit reduces the GTT quantity.
  - A failed delete still closes the trade and flags `gtt_status = 'missing'`.
- **Reconciliation:**
  - Triggered and filled → exit recorded as `premium_stop`.
  - Triggered and unfilled → `gtt_status = 'triggered'`, counted unprotected.
  - A GTT deleted outside the app → `missing`.
  - ~~An untracked Kite position → amber notice.~~ Not implemented (top-of-doc note).
- **IP error:** a rejection whose message matches the IP-error heuristic sets `GET /status`'s `orders.ip_ok = false` and shows the message verbatim; any other rejection sets it `true` (the round-trip to Kite itself worked).
- **Isolation:** no MCP tool or `/api/v1` route can reach `place_order`, `cancel_order` or `place_gtt` (`/api/v1/order/*` doesn't exist at all; only `/ui/order/*` does, CSRF-protected). `modify_gtt` is reachable only via tighten (both `/api/v1` and `/ui`) and loosen (`/ui` only), both of which are also tested to still only allow protective/explained changes.
- **Switches:** `orders.enabled = false` blocks Confirm (`ORDERS_DISABLED`) while Review still works (preview only); `allow_market = false` rejects `order_type="MARKET"` at review (`MARKET_NOT_ALLOWED`).

Implemented in `tests/test_orders_gtt.py` (pure GTT math), `test_orders_mock.py` (mock
client order/GTT simulation), `test_orders_service.py` (the bulk of the checklist above)
and `test_orders_api.py` (isolation + IP-error detection) -- 46 tests, on top of the
existing 157 (203 total as of this writing).

---

#### 7.8.10 Milestone

**M9: Order ticket.**
✅ From a TCS check card, **Buy** opens a ticket for `TCS NOV 2500 CE`: quantity in lots capped at `max_lots`, LIMIT at the ask, NRML, stop at the configured default. Review shows the risk box; Confirm places the order.
✅ The fill is recorded at the average price, and a GTT stop at fill × (1 − stop_pct) is placed (verified against the mock; a real Kite GTT list is unverified without a static IP).
✅ Tightening the stop on Positions moves the GTT; loosening needs a reason.
✅ When the GTT fires and fills, the next sync (or Positions reconciliation) records the exit as `premium_stop`.
✅ A double-click on Confirm places one order. A trade over `max_lots` never reaches `place_order`.
⚠️ Without a whitelisted static IP, live order placement against the real Kite API hasn't actually been exercised -- only the mock path has. Reconciliation and quotes work read-only from any IP, per Kite's own rules, and would continue to once a real session exists.

**Sources checked (Nov 2026, per the original spec): Zerodha support (static IP, GTT behaviour); kite.trade forum (order-endpoint IP scope, market-protection requirement, 10 OPS limit) -- not re-verified during this implementation pass.**

---

### 7.9 Option theses (added, implemented)

> Implemented close to this spec in full -- config, the Monte Carlo and implied-spot
> pricing, the mock client's minute bars, the DB tables and settable-once-from-null
> immutability trigger, the pure resolution/scoring/exit-policy modules, the sync step
> (folded into the shared `optionguard/sync.py` login/button/CLI job), the scorecard's
> policy comparison block, the order-ticket `thesis_id` integration (7.8), MCP tools and
> the Option theses web screen. See implementation note #8 at the top of this doc for the
> one known gap (the path chart has no underlying overlay) before relying on this section
> as a description of exactly what's running.

**Purpose:** test short-term trade ideas on a specific option contract: an entry, a target premium, a stop premium and a few trading days. Score them from the option's own price data. The shorter horizon builds a usable sample (20–30 theses) in weeks rather than months. The score centres on **expectancy** (whether wins outweigh losses), because the user's historical problem is several small wins wiped out by one large loss.

**Non-goals:** no prediction or recommendation. Nothing here blocks a trade beyond the existing guardrails. No order placement outside the 7.8 ticket.

---

#### 7.9.1 Concepts

- **An option thesis** is a planned trade on one contract: entry, target premium, stop premium, duration in trading days, a one-line reason, sector, and a snapshot of the check (7.2) at entry.
- **Mode: `paper | real`.**
  - **Paper:** no order. The entry is the ask at creation time. The outcome is resolved from the option's minute candles (7.9.5). Never counts toward the monthly loss limit or cooling-off.
  - **Real:** linked to a `trades` row. The outcome is the actual fill and exit, from the 7.8 order flow and reconciliation, or from a trade recorded manually.
  - The word **"mock" is not used for this.** In this app, `mock` means simulated Kite data (`data_source`). Theses with `data_source = 'mock'` can be created for testing but are excluded from resolution and scoring.
- **Exit rule, fixed at creation:** whichever comes first of target, stop, or **time exit** (the close of the last trading day of the duration). A manual early exit is allowed, with a reason.
- **One open option thesis per contract** (symbol + expiry + strike + CE/PE), across both modes. A new one is allowed once the previous one is closed or cancelled.
- **Immutable after creation**, except `notes`. Cancelling is web UI only and needs a reason (≥ 15 characters); cancelled theses are excluded from scores but counted.
- **Status:** `open` → `closed` (outcome known) → `scored`. Or `open` → `cancelled`.

---

#### 7.9.2 Config

```toml
[option_theses]
min_duration_days      = 1       # trading days
max_duration_days      = 20
hold_cutoff_calendar_days = 7    # hold cutoff = last trading day on or before (expiry − this many calendar days); durations must end by it
expiry_warning_days    = 3       # warn when an unresolved thesis's contract expires within this many calendar days
paper_slippage_pct     = 0.01    # applied against the user on paper exits (sell lower, stop worse)
late_exit_flag_days    = 1       # a manual exit entered later than this after its exit time is flagged
mc_paths               = 20000   # Monte Carlo paths for mkt_prob_target_first (7.9.4)
mc_steps_per_day       = 25      # ≈ 15-minute steps
mc_seed                = 7
findings_min_n         = 10

[option_theses.policies]    # exit policies scored side by side (7.9.7)
grace_days             = 3       # trading days, counting the entry day as day 1
disaster_stop_pct      = 0.60    # during grace, exit only if the option falls this far below entry
time_checkpoints       = [5, 8, 11]   # trading days, entry day = day 1
time_stop_min_gain_pct = 0.0     # at a checkpoint's close, exit unless the option is at least this far above entry
```

**Sectors** reuse the existing 7.7 sector list and its benchmark indices, unchanged.

---

#### 7.9.3 Input

The form's first step is the contract field and a **Run check** button. It renders the same card partial as the Check tab, including alternatives (7.4). The remaining fields appear below the card.

| Field | Type | Rules |
|---|---|---|
| `contract` | text | `SYMBOL MON STRIKE CE\|PE`, resolved like checks (section 5). 409 if an open option thesis exists for the contract |
| `mode` | `paper \| real` | Radio button; default `paper` |
| `why` | text | Required, 10–200 characters: the one-line reason for the trade |
| `target_premium` | numeric | `> entry_premium`; tick-rounded |
| `stop_premium` | numeric | `< entry_premium`, `> 0`; tick-rounded. For `real`, no looser than the settings stop (section 9) |
| `duration_days` | int | Within config limits; `end_date` must fall on or before the **hold cutoff date** (7.9.7). This keeps every thesis out of the last week before expiry, when physical-settlement margins rise |
| `lots` | int | Default 1 for paper (capped at the check's `max_lots`, with a warning if over); for real, taken from the fill |
| `sector` | text | From the sector list, pre-filled from `[symbol_sectors]` |
| `override_reason` | text | **Required (≥ 15 characters) if the check verdict is red.** Stored in `overrides` with rule `RED_VERDICT_THESIS` |
| `notes` | text, optional | The only editable field |

**Captured automatically:**
- `created_at` (entry date and time).
- `check_id`: the check just run, or one under 30 minutes old.
- A check snapshot: `verdict`, `flags`, `spot`, `bid`, `ask`, `iv`, `delta`, `theta_per_day_lot`, `prob_profit`, `spread_pct`, `lot_size`.
- `entry_premium`: the **ask** for paper (LTP if there's no ask, flagged `entry_from_ltp`); the fill price for real.
- `end_date`: computed with the holiday calendar.
- Sector and NIFTY 50 levels at entry.
- `data_source`.

Paper entries outside market hours use the last ask and are flagged `entry_offhours`. They resolve from the next session's first candle.

---

#### 7.9.4 Derived numbers (shown live on the form, stored on save)

| Field | Definition |
|---|---|
| `reward_risk` | `(target − entry) / (entry − stop)` |
| `risk_per_lot` | `(entry − stop) × lot_size` (₹) |
| `loss_vs_cap` | `risk_per_lot × lots / max_loss_per_trade`. A warning above 1.0 (paper); rejected for real (the 7.8 guardrails apply) |
| `spot_for_target`, `spot_for_stop` | The underlying price at which the Black-Scholes value equals the target or stop, at the midpoint of the duration with IV held constant. Solved with Brent's method; shown as ₹ and % from spot |
| `mkt_prob_target_first` | Monte Carlo: simulate the underlying under the risk-neutral lognormal at `iv` for `duration_days`, with `mc_steps_per_day` steps; reprice the option at each step; count paths where the option reaches the target before the stop. Deterministic seed. Paths that hit neither are counted separately as `mkt_prob_time_exit` |
| `mkt_prob_stop_first` | `1 − target_first − time_exit` |

Form copy: "Market-implied odds: target first X%, stop first Y%, neither by day N Z%. These are risk-neutral odds from today's IV, not a forecast."

---

#### 7.9.5 Resolving outcomes

**Paper theses** are resolved from the option's **minute candles** (Kite `historical_data(token, from, to, "minute")`). Minute candles are needed because daily candles can't tell which came first when both the target and the stop were touched on the same day.

- **Bars:** fetched for each open paper thesis from `created_at` to the earlier of now or the **hold cutoff date** (7.9.7), 15:30 IST. The window runs past `end_date` because the `hold` policy needs it. Stored in `option_minute_bars`, shared across theses on the same contract.
  - Fetched during the sync run on Kite login and by the **Resolve & score** button.
  - **They must be fetched before the contract expires;** Kite has no candles for expired contracts. In practice that means syncing at least once **between the hold cutoff date and expiry**. Any sync in that window completes everything. `expiry_warning_days` (7.9.2) covers this.
- **Walk the bars in time order, from the first full minute after entry:**
  - If a bar's **open** is at or beyond the stop or target (a gap), exit at the open, minus slippage.
  - Otherwise, `low ≤ stop` → exit at the stop. `high ≥ target` → exit at the target.
  - **If both happen within one bar, the stop wins** (conservative), and the thesis is flagged `ambiguous_bar`.
  - Apply `paper_slippage_pct` against the user on every exit.
  - If neither happens by the last bar of `end_date`, it's a **time exit** at that bar's close.
- A paper thesis closes **as soon as** its target or stop is found in the bars; it doesn't wait for `end_date`.
- **Manual early exit (paper):** web UI only. Exit time, price and reason (`judgement | news | other`, plus text).
  - The price must fall within the low–high of that minute's bar, or that day's bar if minute data is missing; otherwise 422.
  - Entered more than `late_exit_flag_days` after the exit time → flagged `late_entry`.
  - Only allowed before the bars show a target or stop.
- **Missing data:** if minute bars are incomplete (the contract expired before fetching), fall back to daily bars, with same-day target and stop counted as stop-first. Mark `bars = 'daily_fallback'`. If no bars exist at all → `outcome = 'unresolvable'`, excluded from scores but counted.

**Real theses:**
- The outcome comes from the linked `trades` row: entry at `entry_premium` (the fill), exit at `exit_premium`, and the reason mapped from `exit_reason`: `target` → target, `premium_stop` → stop, `time_stop` → time, anything else → manual.
- **Creating** a real thesis offers **Save & buy**: the 7.8 entry ticket opens pre-filled with the thesis's lots, target and stop, so the GTT is OCO with those exact levels. On fill, the thesis is linked to the new trade and `entry_premium` is updated to the fill.
- Until orders are enabled (static IP), a real thesis can instead be linked to a trade recorded with **Record a trade placed elsewhere**, within the same trading day. A real thesis with no linked trade by the end of its entry day is auto-cancelled with the reason "no trade linked".
- If the actual exit differs from the thesis's levels (a moved stop, a manual exit), the score records both the planned and the actual outcome.

**After exit, both modes:** bar tracking continues until `end_date` to compute `target_after_stop` (the stop fired, then the target was reached before `end_date`) and the option's best and worst moves over the full window.

---

#### 7.9.6 Scoring (button: **Resolve & score**)

There is no cron job. The button on the Option theses tab (and the sync on Kite login) does four things:
1. Fetch missing minute bars for every non-mock thesis whose contract hasn't expired (paper and real; 7.9.7 needs bars for both).
2. Resolve outcomes for open theses.
3. Score every `closed` thesis whose tracking window has ended (`end_date` passed), or immediately if `end_date` is today and it's after 15:45 IST.
4. Compute or recompute the policy results (7.9.7) for every scored thesis. A policy whose exit falls after the bars fetched so far (typically `hold`, which runs to the hold cutoff date) is stored with `outcome = NULL` and `pending = true`, and completed by any later run once the bars reach that date. If the contract expires before that happens, the row becomes `unresolvable`. The scorecard counts pending rows separately and leaves them out of that policy's numbers.

It's idempotent and uses `optionguard/sync.py`'s advisory lock and `sync_runs`.

**Per-thesis score:**

| Field | Definition |
|---|---|
| `outcome` | `target \| stop \| time \| manual \| unresolvable` |
| `exit_time`, `exit_premium` | As resolved |
| `days_held` | Trading days from entry to exit (0 = same day) |
| `ret_pct` | `(exit − entry) / entry` |
| `pnl` | `(exit − entry) × lots × lot_size − charges` (₹). Charges are the config estimate for paper, actual for real |
| `r_multiple` | `(exit − entry) / (entry − stop)`. +2.0 = won twice the planned risk; −1.0 = lost exactly the planned risk; below −1 = a gap or slippage past the stop |
| `mfe_pct`, `mfe_time` | The option's best price vs entry, over the full window to `end_date` |
| `mae_pct`, `mae_time` | The option's worst price vs entry, over the full window |
| `target_after_stop` | The stop fired, then the target was reached before `end_date` |
| `planned_vs_actual` | Real only: the planned outcome (from bars) against the actual one, and the R difference |
| `spot_ret_pct`, `sector_ret_pct`, `market_ret_pct` | Over entry to exit |
| `flags` | `ambiguous_bar`, `late_entry`, `entry_offhours`, `entry_from_ltp`, `daily_fallback` |

---

#### 7.9.7 Exit policy comparison

**Purpose:** the thesis's own outcome (7.9.5) uses one exit rule, the plain stop. To find out whether a different rule would have done better, every thesis is also replayed under several exit policies on the **same bars**. Nothing extra is entered. The scorecard compares the policies side by side.

**Common rules for every policy:**
- The target is active from entry, in every policy.
- Paper fills follow 7.9.5: gaps exit at the open, a stop and target in the same bar count as stop, and slippage applies.
- Day 1 is the entry day; days are trading days.
- All policies use the same entry price and lots, so ₹ results are directly comparable. `r_multiple` always uses the thesis's planned risk, `entry − stop_premium`, so R is comparable too.

**Policies:**

| Key | Rule |
|---|---|
| `plain` | Stop at `stop_premium` from entry; time exit at `end_date` close. This matches the thesis's own outcome (7.9.5) |
| `grace3` | Days 1 to `grace_days` (3): only a disaster stop at `entry × (1 − disaster_stop_pct)`. From day `grace_days + 1`: the normal `stop_premium`. Time exit at `end_date` close |
| `time_d5`, `time_d8`, `time_d11` | `plain`, plus at the **close** of each checkpoint day up to and including N (one variant per value in `time_checkpoints`): exit at that close if the option is below `entry × (1 + time_stop_min_gain_pct)`. Checkpoints after `end_date` are skipped, and a variant whose N is past `end_date` isn't computed |
| `hold` | No stop. Target active. If the target isn't hit, exit at the close of the **hold cutoff date**: the last trading day on or before `expiry − hold_cutoff_calendar_days` calendar days. This models the past habit of holding a losing option until there's no choice but to exit |

**Hold cutoff date.** For a 29 Dec expiry with `hold_cutoff_calendar_days = 7`, the cutoff is 22 Dec. If 22 Dec isn't a trading day, it's the trading day before. The date is **fixed by the contract**, not by when the user syncs: any sync between the cutoff and expiry fetches bars up to the cutoff and finalises `hold`. The user doesn't need to run the app in the last week, and two identical theses always get the same `hold` result.

The cutoff sits before the final week because Zerodha raises margins on in-the-money stock options as physical settlement approaches. An out-of-the-money option's value at the cutoff is usually small, which is exactly the wipeout this baseline exists to show.

**`hold` is always hypothetical.** It's the same mechanical rule for paper and real theses. An actual exit after the cutoff date (e.g. a real trade held longer with a raised target) is **not** used as the `hold` value. It's recorded only as the real trade's actual outcome (`planned_vs_actual`, 7.9.6).

**Real theses:** the policies are replayed on the minute bars exactly as for paper. The actual outcome from the fill stays separate, as `planned_vs_actual` (7.9.6). Replaying needs the bars, so real theses also get bars fetched.

**Form display:** under the derived numbers (7.9.4), show ₹ risk per lot at the **disaster stop** next to the normal stop, so the user can see what the grace period costs if it goes wrong. Sizing for a real trade still uses the normal stop (section 7.3); this display is information only.

**Result per policy:** `outcome` (`target | stop | disaster | time_checkpoint | time | hold_end`), `exit_time`, `exit_premium`, `days_held`, `pnl`, `r_multiple`. Stored in `option_thesis_policy_results`, one row per thesis per policy.

**For real GTTs later:** `grace3` maps directly onto the existing tighten-only rule. Place the GTT at the disaster stop at entry, then tighten it to the normal stop on day 4. Not part of this milestone. Note it in 7.8 if the comparison favours `grace3`.

---

#### 7.9.8 Scorecard (`GET /api/v1/option-theses/scorecard`)

Query parameters: `from_date`, `to_date`, `mode = paper | real | all`, `group_by = sector | verdict | outcome | symbol | opt_type | override | month | none`. Unresolvable, cancelled and mock theses are excluded and counted.

**Per group:**
- `n`
- `win_rate` (pnl > 0) and `target_rate`
- `avg_win`, `avg_loss` (₹ and R), `payoff_ratio` (avg win ÷ |avg loss|)
- **`expectancy`**: mean pnl per thesis (₹), and mean R
- `largest_loss`, and **`largest_loss_share`** = largest single loss ÷ sum of all wins. This is the "one loss wipes out the wins" detector
- `stop_rate`, `time_exit_rate`, `manual_rate`, `median_days_held`
- **`target_rate` vs mean `mkt_prob_target_first`**: did targets hit more often than the market's odds implied?
- `target_after_stop_rate`: are stops too tight?
- `median_mfe_pct` on stopped and time-exit theses: how much was available but not taken

**Policy block** (7.9.7): for each policy, over the same set of theses: `n`, `win_rate`, `avg_win`, `avg_loss`, **`expectancy`** (₹ and R), `largest_loss`, **`largest_loss_share`**, `median_days_held`, and the outcome mix. Also a pairwise line against `plain`: on how many theses the policy did better or worse, and the ₹ total of the difference. The thesis's own outcome is the `plain` row.

**Independence note:** the scorecard also shows the number of distinct entry dates and of distinct (entry date, sector) pairs. Five IT theses opened on one day count as roughly one observation of the market, and the findings say so.

**Findings:** plain sentences, emitted only when `n ≥ findings_min_n`. For example:
- "Expectancy +₹420 per thesis (+0.21R) over 18 paper theses."
- "Targets hit 33% vs 26% market-implied."
- "Largest loss equals 61% of all wins."
- "Red-verdict theses: expectancy −₹950 (n=6) vs green +₹610 (n=9)."
- "Stopped theses later reached target in 5 of 9: stops may be too tight."
- "Policy grace3: expectancy +₹310 vs plain +₹120; better on 6 theses, worse on 3 (net +₹3,420)."
- "Policy hold: expectancy −₹1,180; largest loss equals 140% of all wins."

---

#### 7.9.9 Database (PostgreSQL)

See section 10 for the actual `CREATE TABLE` statements (merged in there alongside the
rest of the schema) and the immutability trigger. Summary: `option_theses`,
`option_minute_bars`, `option_thesis_scores`, `option_thesis_policy_results` -- four
tables, matching the column lists in 7.9.3/7.9.4/7.9.5/7.9.6/7.9.7 exactly.

**Immutability:** the repository layer and a trigger allow updates only to `notes`,
`status`, the outcome and exit columns (each settable once, from null), `trade_id`
(settable once, real only), the cancel columns, and `entry_premium` (real only, once, when
linked to the fill).

---

#### 7.9.10 API, MCP, UI

**API (`/api/v1/option-theses`):**

| Method | Path | Purpose |
|---|---|---|
| POST | `/option-theses/preview` | Takes contract, target, stop, duration and lots; returns the derived numbers (7.9.4) without saving. Used by the live form |
| POST | `/option-theses` | Create. `mode = real` returns `next: "/ui/order?check_id=…&thesis_id=…"` |
| GET | `/option-theses` | Filters: `status`, `mode`, `sector`, `symbol`, `verdict`, `from_date`, `to_date` |
| GET | `/option-theses/{id}` | Thesis, score, and interim progress if open (live premium vs target and stop, days left) |
| PATCH | `/option-theses/{id}/notes` | Notes only |
| POST | `/option-theses/resolve` | Resolve & score (7.9.6); 409 if a sync is running; requires a Kite session |
| GET | `/option-theses/scorecard` | 7.9.8, including the policy block |
| — | `/ui/option-theses/{id}/exit` (POST) | **UI-only.** Manual paper exit (CSRF) |
| — | `/ui/option-theses/{id}/cancel` (POST) | **UI-only.** Cancel with reason (CSRF) |
| — | `/ui/option-theses/{id}/link-trade` (POST) | **UI-only.** Link a real thesis to a manually recorded trade from the same day (CSRF) |

**Order ticket (7.8) integration:** the entry ticket accepts `thesis_id`. When present, it pre-fills lots, stop and target from the thesis (the user can tighten but not loosen them) and makes the GTT OCO. On fill it links `trade_id` and sets `entry_premium` to the fill.

**MCP tools:**
- `preview_option_thesis(...)` and `add_option_thesis(...)`, **paper only**. Real mode needs the web UI.
- `list_option_theses(...)`, `get_option_thesis(id)` and `option_thesis_scorecard(...)`.
- Tool descriptions must say: ask the user for the target, stop, duration and reason; never fill them in yourself; report the market-implied odds; ask for an override reason if the verdict is red.
- No exit, cancel, link or resolve tools.

**Web UI, a new tab, Option theses (`/option-theses`):**
- **Status bar:** last resolve/sync time, a **Resolve & score** button, and the expiry warning when a thesis's contract expires within `expiry_warning_days` and its bars don't yet reach the hold cutoff date ("Sync before 29 Dec or the hold result for 2 theses is lost").
- **New thesis panel:**
  - Contract field and **Run check**, rendering the check card with alternatives.
  - Mode radio (paper / real), `why`, target, stop, duration, lots and sector.
  - Override box, shown only for a red verdict.
  - Notes.
  - A live derived panel (7.9.4): reward:risk, ₹ risk vs cap, stock price needed for the target and stop, and market odds of target first, stop first or neither.
  - **Save (paper)** or **Save & buy (real)**.
- **Open:** one row per thesis, with a bar showing stop, entry, current premium and target; days left; mode tag; verdict chip.
- **Closed and scored:** outcome, days held, ₹ P&L, R, MFE/MAE, a target-after-stop marker and flags. Clicking a row opens a path chart of the option (15-minute closes) with horizontal lines at stop, entry and target, the exit point marked, and the underlying on a second axis.
- **Scorecard:** mode and grouping selectors, the metrics in 7.9.8, the policy comparison table (one row per policy), the independence note, and findings.

---

#### 7.9.11 Tests

- **Validation:**
  - Stop ≥ entry, or target ≤ entry → 422.
  - Duration out of bounds, or `end_date` inside the expiry buffer → 422.
  - Red verdict without an override reason → 422; with a reason, an `overrides` row is created.
  - A second open thesis on the same contract → 409. Different strikes are fine.
- **Derived numbers:**
  - `reward_risk` and `risk_per_lot` are exact.
  - `spot_for_target` reprices to the target within a tolerance.
  - The Monte Carlo is deterministic for a fixed seed. Target-first, stop-first and neither sum to 1. Moving the target farther away lowers the target-first probability.
- **Paper resolution (pure, fixture bars):**
  - Clean target; clean stop; time exit at the last bar's close.
  - Gap-open through the stop exits at the open, not the stop.
  - Both in one bar → stop, with `ambiguous_bar`.
  - Slippage is applied against the user.
  - Resolution happens as soon as a level is hit, before `end_date`.
  - `target_after_stop` is detected.
  - Daily fallback is flagged.
- **Manual exit:**
  - A price outside the bar's range → 422.
  - A late entry is flagged.
  - Not allowed after the bars show a resolved outcome.
- **Real:**
  - Save & buy pre-fills the ticket with the thesis levels, and the GTT is OCO.
  - A fill links the trade and updates `entry_premium`.
  - A stop-triggered GTT reconciles to `outcome = stop`.
  - An unlinked real thesis auto-cancels at the end of its day.
  - `planned_vs_actual` is computed when the user moved the stop.
- **Isolation:**
  - Paper theses never affect monthly P&L or cooling-off.
  - MCP can't create real theses or reach exit, cancel or link.
  - Mock-data theses are never resolved or scored.
- **Scorecard:**
  - Expectancy, payoff ratio, R and `largest_loss_share` are correct on a fixture set (including a "four small wins, one big loss" case giving negative expectancy).
  - Findings are suppressed below `findings_min_n`.
  - The independence counts are correct.
- **Exit policies (pure, fixture bars):**
  - A path that dips past the normal stop on day 2 and then reaches the target on day 6: `plain` = stop, `grace3` = target.
  - A path that falls through the disaster level on day 1: `grace3` = disaster, with a larger loss than `plain`.
  - A flat, decaying path: `time_d5` exits at the day-5 close; `plain` exits at `end_date`; `hold` exits at the hold cutoff date's close with the smallest value.
  - A time checkpoint is skipped when the option is above entry × (1 + min gain) at that close.
  - Checkpoint variants past `end_date` aren't computed.
  - The hold cutoff date for a 29 Dec expiry is 22 Dec, or the previous trading day when 22 Dec is a holiday or weekend.
  - `hold` stays `pending` until bars reach the cutoff date, and is excluded from the scorecard until then. A sync on any day from the cutoff to expiry finalises it, with the same result whichever day it runs.
  - A contract that expires before the bars reach the cutoff → `hold` is `unresolvable`.
  - A duration ending after the hold cutoff date → 422.
  - A real trade exited after the cutoff doesn't change `hold`.
  - `plain` always matches the thesis's own outcome.
  - R uses the planned risk (`entry − stop_premium`) in every policy, including `grace3` and `hold`.
- **Idempotency:** running Resolve & score twice gives identical scores and policy rows; a concurrent run → 409. Changing `time_checkpoints` and re-scoring updates the policy rows and records the new parameters.

---

#### 7.9.12 Milestone

**M10: Option theses.**
✅ On the Option theses tab, the user enters `TCS DEC 3300 CE` and clicks **Run check**; the check card renders with alternatives. The user sets paper mode, target 85, stop 45, 5 days, 1 lot, sector IT and a reason, and sees reward:risk, ₹ risk vs cap and the market's target-first odds before saving.
✅ A red-verdict contract can't be saved without an override reason.
✅ On a later day, **Resolve & score** (or the Kite-login sync) fetches minute bars, resolves the thesis as target, stop or time from the bars, and scores it with ₹ P&L, R, MFE/MAE and target-after-stop. The scorecard shows expectancy and `largest_loss_share` for paper theses.
✅ The same thesis appears in the policy table under `plain`, `grace3`, `time_d5`/`d8`/`d11` (where within the duration) and `hold`; `hold` shows as pending until a sync on or after the hold cutoff date (e.g. 22 Dec for a 29 Dec expiry) fetches bars up to that date.
✅ With orders enabled, **Save & buy** on a real thesis opens the order ticket pre-filled with the thesis levels. The fill links to the thesis, and when the OCO GTT fires the thesis closes as target or stop.

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
| `record_trade` | **Hard:** monthly realised loss < `monthly_loss_limit`; not in cooling-off; `lots ≤ max_lots` from the linked check; check is < 30 minutes old. **Overridable with reason:** verdict is red. Section 7.8's order ticket reuses this exact function for both Review and Confirm, rather than re-implementing it. |
| `tighten_stop` | New stop must be more protective: higher premium stop, or a thesis level closer to spot; or an earlier time-stop date. Otherwise reject. Section 7.8.5, added: also moves the real GTT (via `orders/service.py`'s `sync_gtt_after_stop_change`) if the trade has one. |
| `loosen_stop` | **Web UI only** (no API route reachable from MCP; see section 11), requires a reason, and is logged in `overrides`. Also moves the real GTT, same as tighten (7.8.5). |
| `update_settings` | Web UI only. A change that loosens any rule gets `effective_from` = next trading day 09:00 IST. A tightening change is effective immediately. |
| `clear_cooloff` | Not available. |
| `review_entry` / `review_exit` / `confirm_entry` / `confirm_exit` | Section 7.8.4, added. New checks beyond `record_trade`: `guard_order_price` (limit within ±5% of live LTP), `guard_no_live_order_intent` (no second live intent on the contract), `guard_stop_no_looser_than_settings`, `guard_intent_not_expired` (2-minute review TTL), `guard_exit_quantity` (≤ open quantity, trade still open), `guard_orders_enabled`, `guard_order_type_allowed` (MARKET needs `allow_market`). |
| `review_entry` **with `thesis_id`** | Section 7.9.10, added. `guard_thesis_tighten_only`: lots/stop/target may be tightened from the linked real option thesis's planned values but never loosened -- overridable by nobody, hard reject. |

- **Cooling-off** starts when cumulative realised P&L for the calendar month ≤ −`monthly_loss_limit`, and lasts `cooloff_days` trading days.
- **Monthly P&L** is always computed from `trades`, never stored.
- **Route separation:** MCP-callable routes are under `/api/v1/*`. UI-only, loosening routes are under `/ui/*` and require a CSRF token issued to the browser session. The MCP server only knows `/api/v1`.

---

## 10. Database schema (PostgreSQL -- not SQLite, see implementation note at top)

No `alerts` table: the monitor/alerting subsystem is not implemented in v1 (section 12).
Everything else below matches the original design, translated to Postgres (`SERIAL`,
`JSONB`, native `BOOLEAN`), plus `delta`/`daily_move_needed` added for section 7.6, plus
`sync_runs`, `order_intents` and four new `trades` columns added for section 7.8, plus the
four `option_theses*` tables added for section 7.9. The actual source of truth is
`src/optionguard/db/models.py` plus the Alembic migrations in `alembic/versions/`. Two
section-7.6 fields are deliberately *not* columns, per that section's own spec:
`daily_move_needed_pct` is derived at read time (`daily_move_needed / spot`, both already
available), and `scenarios` lives inside `raw`.

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
  delta NUMERIC, daily_move_needed NUMERIC,  -- section 7.6, added; daily_move_needed_pct
                                              -- and scenarios are NOT columns -- see below
  stretch_5d_pct NUMERIC, stretch_20dma_pct NUMERIC, spread_pct NUMERIC,
  thesis_target NUMERIC, thesis_date DATE,
  verdict            TEXT NOT NULL CHECK (verdict IN ('red','amber','green')),
  flags              JSONB NOT NULL,          -- [{code, level, message}]
  max_lots           INTEGER NOT NULL,
  alternatives       JSONB,
  oi_context         JSONB,
  raw                JSONB NOT NULL,          -- full inputs + outputs, incl. raw.scenarios
                                              -- (section 7.6's grid -- not its own column)
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
  pnl                NUMERIC,                 -- (exit-entry)*lots*lot_size - charges
  entry_intent_id    INTEGER REFERENCES order_intents(id),  -- section 7.8, added (use_alter: see below)
  exit_intent_id     INTEGER REFERENCES order_intents(id),  -- section 7.8, added (use_alter: see below)
  gtt_id             BIGINT,                                -- section 7.8, added
  gtt_status         TEXT CHECK (gtt_status IN ('active','missing','triggered','deleted'))  -- section 7.8, added
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

-- The original schema here also had theses/thesis_drivers/thesis_option_bars/
-- thesis_scores for section 7.7's stock-price thesis journal, since removed
-- (superseded by section 7.9's option theses, below) -- dropped by migration,
-- along with their immutability triggers. sync_runs, created in that same
-- original migration, stayed: it's shared sync-pipeline infrastructure, not
-- thesis-specific, and 7.9's sync still writes to it.
CREATE TABLE sync_runs (
  id           SERIAL PRIMARY KEY,
  started_at   TIMESTAMPTZ NOT NULL,
  finished_at  TIMESTAMPTZ,
  trigger      TEXT NOT NULL CHECK (trigger IN ('login','button','cli')),
  status       TEXT NOT NULL CHECK (status IN ('running','ok','partial','failed')),
  -- counts: {checks_settled, exits_recorded, unprotected, intents_resumed,
  -- option_bars_upserted, option_theses_auto_cancelled, option_theses_resolved,
  -- option_theses_scored, option_policies_computed}
  counts       JSONB,
  errors       JSONB    -- [{thesis_id|check_id|trade_id|intent_id, step, message}]
);

-- Section 7.8, added. See 7.8.7 for field-by-field definitions.
CREATE TABLE order_intents (
  id              SERIAL PRIMARY KEY,
  created_at      TIMESTAMPTZ NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('entry','exit')),
  check_id        INTEGER REFERENCES checks(id),
  trade_id        INTEGER REFERENCES trades(id),
  tradingsymbol   TEXT NOT NULL,   -- app-internal dedup key, not Kite's own tradingsymbol -- see 7.8.7
  quantity        INTEGER NOT NULL,
  order_type      TEXT NOT NULL CHECK (order_type IN ('LIMIT','MARKET')),
  price           NUMERIC,
  stop_premium    NUMERIC,
  target_premium  NUMERIC,
  override_reason TEXT,
  status          TEXT NOT NULL CHECK (status IN
                    ('reviewed','placed','filled','partial','rejected','cancelled','expired')),
  order_id        TEXT,
  filled_qty      INTEGER,
  average_price   NUMERIC,
  kite_message    TEXT,
  raw             JSONB,
  CHECK ((kind = 'entry' AND check_id IS NOT NULL) OR (kind = 'exit' AND trade_id IS NOT NULL))
);
CREATE UNIQUE INDEX ux_order_intents_live_contract
  ON order_intents(tradingsymbol) WHERE status IN ('reviewed','placed');


-- Section 7.9, added. See 7.9.9 for field-by-field definitions.
CREATE TABLE option_theses (
  id                      SERIAL PRIMARY KEY,
  created_at              TIMESTAMPTZ NOT NULL,
  symbol                  TEXT NOT NULL,
  expiry                  DATE NOT NULL,
  strike                  NUMERIC NOT NULL,
  opt_type                TEXT NOT NULL CHECK (opt_type IN ('CE','PE')),
  opt_instrument_token    BIGINT,
  mode                    TEXT NOT NULL CHECK (mode IN ('paper','real')),
  data_source             TEXT NOT NULL CHECK (data_source IN ('manual','mock','kite')),
  sector                  TEXT NOT NULL,
  sector_index            TEXT,
  why                     TEXT NOT NULL CHECK (length(why) BETWEEN 10 AND 200),
  check_id                INTEGER NOT NULL REFERENCES checks(id),
  verdict                 TEXT NOT NULL CHECK (verdict IN ('red','amber','green')),
  check_snapshot          JSONB NOT NULL,
  override_id             INTEGER REFERENCES overrides(id),
  lot_size                INTEGER NOT NULL,
  lots                    INTEGER NOT NULL CHECK (lots >= 1),
  entry_premium           NUMERIC NOT NULL,
  target_premium          NUMERIC NOT NULL,
  stop_premium            NUMERIC NOT NULL,
  duration_days           INTEGER NOT NULL,
  end_date                DATE NOT NULL,
  reward_risk             NUMERIC NOT NULL,
  spot_for_target         NUMERIC,
  spot_for_stop           NUMERIC,
  mkt_prob_target_first   NUMERIC,
  mkt_prob_stop_first     NUMERIC,
  mkt_prob_time_exit      NUMERIC,
  spot_entry              NUMERIC NOT NULL,
  sector_index_entry      NUMERIC,
  market_index_entry      NUMERIC,
  entry_flags             TEXT[] NOT NULL DEFAULT '{}',
  trade_id                INTEGER REFERENCES trades(id),   -- real only
  notes                   TEXT,
  status                  TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open','closed','scored','cancelled')),
  outcome                 TEXT CHECK (outcome IN ('target','stop','time','manual','unresolvable')),
  exit_time               TIMESTAMPTZ,
  exit_premium            NUMERIC,
  manual_exit_reason      TEXT,
  manual_exit_entered_at  TIMESTAMPTZ,
  cancelled_at            TIMESTAMPTZ,
  cancel_reason           TEXT CHECK (cancel_reason IS NULL OR length(cancel_reason) >= 15),
  CHECK (stop_premium < entry_premium AND entry_premium < target_premium),
  CHECK (end_date <= expiry),
  CHECK (mode = 'paper' OR trade_id IS NOT NULL OR status IN ('open','cancelled'))
);
CREATE UNIQUE INDEX ux_option_theses_open_contract
  ON option_theses(symbol, expiry, strike, opt_type) WHERE status = 'open';
CREATE INDEX ix_option_theses_status ON option_theses(status, end_date);

-- Section 7.9, added. One-way reference (option_theses already exists by the time this
-- column is added, unlike the trades.entry_intent_id/exit_intent_id circular pair above),
-- so no use_alter is needed -- a plain ALTER TABLE.
ALTER TABLE order_intents
  ADD COLUMN option_thesis_id INTEGER REFERENCES option_theses(id);

CREATE TABLE option_minute_bars (
  instrument_token  BIGINT NOT NULL,
  ts                TIMESTAMPTZ NOT NULL,
  o NUMERIC, h NUMERIC, l NUMERIC, c NUMERIC,
  volume            BIGINT,
  oi                BIGINT,
  fetched_at        TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (instrument_token, ts)
);

CREATE TABLE option_thesis_scores (
  thesis_id              INTEGER PRIMARY KEY REFERENCES option_theses(id),
  scored_at              TIMESTAMPTZ NOT NULL,
  score_version          INTEGER NOT NULL,
  days_held              INTEGER NOT NULL,
  ret_pct                NUMERIC NOT NULL,
  pnl                    NUMERIC NOT NULL,
  charges                NUMERIC NOT NULL,
  r_multiple             NUMERIC NOT NULL,
  mfe_pct                NUMERIC,
  mfe_time               TIMESTAMPTZ,
  mae_pct                NUMERIC,
  mae_time               TIMESTAMPTZ,
  target_after_stop      BOOLEAN,
  planned_outcome        TEXT,               -- real only: what the bars say would have happened
  planned_r              NUMERIC,
  spot_ret_pct           NUMERIC,
  sector_ret_pct         NUMERIC,
  market_ret_pct         NUMERIC,
  bars                   TEXT NOT NULL CHECK (bars IN ('minute','daily_fallback','none','fill')),
  flags                  TEXT[] NOT NULL DEFAULT '{}',
  path                   JSONB                -- downsampled (15-min) option closes, for the chart
);

CREATE TABLE option_thesis_policy_results (
  thesis_id      INTEGER NOT NULL REFERENCES option_theses(id),
  policy         TEXT NOT NULL,             -- plain | grace3 | time_d5 | time_d8 | time_d11 | hold
  policy_params  JSONB NOT NULL,            -- config values used, e.g. {"grace_days":3,"disaster_stop_pct":0.6}
  outcome        TEXT CHECK (outcome IN
                   ('target','stop','disaster','time_checkpoint','time','hold_end','unresolvable')),
  exit_time      TIMESTAMPTZ,
  exit_premium   NUMERIC,
  days_held      INTEGER,
  pnl            NUMERIC,
  r_multiple     NUMERIC,
  pending        BOOLEAN NOT NULL DEFAULT FALSE,   -- exit lies beyond the bars fetched so far
  scored_at      TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (thesis_id, policy)
);

-- Immutability, second line of defence after db/repo.py's update_option_thesis() (7.9.9).
-- Most fields are fully immutable; outcome/exit_time/exit_premium/manual_exit_reason/
-- manual_exit_entered_at/trade_id/cancelled_at/cancel_reason are each settable exactly
-- once (null -> a value); entry_premium only while mode='real' and trade_id IS NULL (the
-- fill-price update, done in the same statement that sets trade_id). Hand-written in the
-- migration -- abbreviated here for brevity (the actual function checks each column).
CREATE FUNCTION option_theses_enforce_immutability() RETURNS trigger AS $$
BEGIN
  IF NEW.* IS DISTINCT FROM OLD.* -- (actual migration lists every non-mutable column)
     AND <none of the once-settable or always-mutable columns changed from their prior value>
  THEN
    RAISE EXCEPTION 'option_theses: this field is immutable after creation';
  END IF;
  -- then one IF per once-settable column: "IF OLD.x IS NOT NULL AND NEW.x IS DISTINCT
  -- FROM OLD.x THEN RAISE EXCEPTION '...can only be set once'; END IF;"
  -- and entry_premium's own real-mode-only-before-link check.
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_option_theses_immutable BEFORE UPDATE ON option_theses
  FOR EACH ROW EXECUTE FUNCTION option_theses_enforce_immutability();
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
| POST | `/trades/{id}/tighten` | Body: any of the three stop fields; tighten only. Section 7.8, added: also moves the trade's real GTT if it has one |
| POST | `/trades/{id}/exit` | Body: `{exit_premium, exit_reason, charges?}` |
| GET | `/journal/stats` | Query: `from, to, group_by=verdict\|exit_reason\|month\|symbol\|override` |
| GET | `/journal/checks` | Paged list, including untraded checks with `expiry_settle` |
| GET/POST | `/events` | List or add corporate events |
| GET | `/status` | `kite_mode: "mock"\|"live"`, Kite login state, month P&L, loss-limit headroom, cooling-off state |
| GET | `/settings` | Active settings (read-only) |
| — | `/ui/settings` (POST) | **UI-only.** Update settings |
| — | `/ui/trades/{id}/loosen` (POST) | **UI-only.** Requires a reason |
| — | `/kite/login`, `/kite/callback` | Auth flow. `/kite/callback` also kicks off a background sync after storing the token |
| GET | `/sync/latest`, `/sectors` | Generic: the last sync run, and the configured sectors (used by 7.9's sector picker) |
| GET | `/orders/intents` | Section 7.8, added, read-only. Filterable by `status` |
| — | `/ui/order/review`, `/ui/order/confirm`, `/ui/order/{id}/status` (GET), `/ui/order/{id}/modify`, `/ui/order/{id}/cancel`, `/ui/trades/{id}/gtt` (all POST except status) | **UI-only**, section 7.8 -- full table in 7.8.8. None of these exist under `/api/v1`; that's deliberate (7.8.1) |
| POST | `/option-theses/preview` | Section 7.9.10, added. Live derived numbers for a planned option thesis, without saving |
| POST/GET | `/option-theses`, `/option-theses/{id}`, `/option-theses/scorecard` | Section 7.9, added -- full table in 7.9.10. `mode = real` on create returns `next: "/ui/order?check_id=…&thesis_id=…"` |
| PATCH | `/option-theses/{id}/notes` | Section 7.9 |
| POST | `/option-theses/resolve` | Section 7.9.6's Resolve & score button -- the same sync pipeline as `/sync` (folded in as a new step), not a second one |
| — | `/ui/option-theses/{id}/exit`, `/ui/option-theses/{id}/cancel`, `/ui/option-theses/{id}/link-trade` (all POST) | **UI-only**, section 7.9. None of these exist under `/api/v1`, same isolation rule as 7.8.1 |

`CheckResult` = every metric in section 7.1 (including `delta`, `daily_move_needed`, `daily_move_needed_pct` and `scenarios` from section 7.6), plus `verdict`, `flags`, `max_lots`, `alternatives`, `oi_context`, `explanation_facts` (a list of short factual sentences generated in code, which Claude can quote).

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
>   in-process job. `optionguard/sync.py` folds it into the same `optionguard sync` run as
>   order/GTT reconciliation and section 7.9's option theses pipeline, which also runs
>   automatically on Kite login and from the **Resolve & score** button, behind a Postgres
>   advisory lock -- still not a scheduled in-process job, but closer to "monitor-ish" than
>   anything else in this build gets.
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
   - Result cards: verdict colour, key metrics (including delta and the "move needed/day"
     from section 7.6), flags, max lots, alternatives, OI context, and a 3×5 holding-period
     scenario grid (section 7.6) with P&L cells tinted green/red by sign.
   - Ranking strip at the top.
   - **Buy** (section 7.8, renamed from **Take trade**) opens the order ticket (`/order?check_id={id}`)
     for a kite/mock candidate. The old record-only form moved behind **Record a trade
     placed elsewhere**, still pre-filled with max lots and default stops; a red verdict
     still requires an override reason either way.
   - Input is disabled during cooling-off or at the monthly limit.
   - **Add option thesis** link on each result card, to `/option-theses?check_id={id}&contract=...` -- section 7.9, added.
2. **Positions** (`/positions`)
   - Table of open trades with live P&L and distance to each stop.
   - **Exit via ticket** (section 7.8, added) opens the exit order ticket (`/order?trade_id={id}`)
     for a kite/mock trade. The old manual exit form moved behind **Record an exit placed
     elsewhere**. **Tighten** (now also moves a real GTT, 7.8.5) and **Loosen** (behind a
     confirm dialog asking for a reason, same as before) are unchanged otherwise.
   - A red banner plus **Retry GTT** button when `gtt_status == 'missing'` (section 7.8,
     added); a different banner with a direct exit-ticket link when `gtt_status ==
     'triggered'` (the stop fired at Kite but didn't fill -- position is unprotected).
   - ~~Recent alerts.~~ Not present -- no alerting subsystem in v1 (see implementation note
     at top, and section 12).
   - **GTT check:** for each kite/mock position, a read-only call asks Kite whether an active
     GTT order exists for that exact contract (`KiteProvider.has_gtt`, backed by
     `kite.get_gtts()`). If not, the row shows a warning -- the stop in this app is just a
     number until a real order enforces it. Added, pre-7.8, after a review flagged that
     nothing was actually enforcing exits once the monitor loop was removed; still useful
     post-7.8 as a backstop for manually-recorded trades (no order intent, so no automatic
     GTT) and for a trade whose automatic GTT placement failed. `has_gtt` is
     `null`/not-applicable for manual trades.
3. **Journal** (`/journal`)
   - Stats grouped by verdict, exit reason, month and override.
   - A "skipped checks" table showing what untraded candidates were worth at expiry.
   - No **Sync now** button here -- it's on the Theses screen only (implementation note #6).
4. **Settings** (`/settings`)
   - Risk rules, with a note that loosening applies tomorrow.
   - Corporate events CRUD.
   - Kite login button.
   - Holiday list (read from config, displayed).
5. **Order ticket** (`/order?check_id=…` or `?trade_id=…`, optionally `&thesis_id=…`) --
   section 7.8, added. Opened from Check's **Buy**, Positions' **Exit via ticket**, or
   Option theses' **Save & buy**, never navigated to directly. See 7.8.3 for the full
   layout (live quote strip polled every 3s, quantity/price/stop/target fields, Review →
   risk box → Confirm, then a self-polling fill-status partial with Modify-price/Cancel
   while still open at Kite) and implementation note #7 for what's not there yet (no
   time-of-day/market-hours disabling). With `thesis_id` (section 7.9.10, added): lots,
   stop and target pre-fill from the linked real option thesis and a banner explains the
   tighten-only rule; `guard_thesis_tighten_only` (section 9) enforces it server-side
   regardless of what the form actually submits.
6. **Option theses** (`/option-theses`) -- section 7.9, added. A status bar (last
   resolve/sync time + **Resolve & score** button + expiry warning), a new-thesis panel
   with a live derived-numbers preview, open/closed tables, a path chart with
   stop/entry/target lines and the exit marked, and a scorecard with the policy
   comparison table -- for theses on the option contract's own premium, scored from the
   option's own minute candles. The new-thesis panel's **Run check** reuses the same
   `/checks-html` endpoint and card partial as the Check tab;
   each card's **Add option thesis** link (item 1 above) is how a run check's `check_id`
   gets here. Known gap: see implementation note #8 at the top (no underlying overlay on
   the path chart).

---

## 14. MCP server (`mcp_server/server.py`)

Built on `mcp.server.mcpserver.MCPServer` (the `mcp` 2.x API -- `FastMCP` was renamed; see
section 3). Each tool is a thin wrapper over `/api/v1`. Tool descriptions must tell Claude
to report the computed numbers and flags rather than estimate. Tool definitions are
transport-agnostic and shared by two entrypoints:

- **`mcp_server/server.py`** (`python -m mcp_server.server`) -- stdio transport, per the
  original design. Claude Desktop spawns this itself as a local subprocess; no auth needed
  since there's no network boundary to cross.
- **`mcp_server/http_main.py`** (`python -m mcp_server.http_main`, also the `mcp` service
  in `docker-compose.yml`) -- **added later, not in the original design.** Streamable HTTP
  (`MCPServer.streamable_http_app(stateless_http=True)`) wrapped in a small raw-ASGI
  bearer-token middleware (`MCP_AUTH_TOKEN`, timing-safe comparison), so this can run as a
  long-lived container that Claude Desktop reaches via a stdio-to-HTTP bridge like
  `mcp-remote`, instead of being spawned as a local subprocess -- mirrors the pattern (and
  the reasoning for a static shared secret over full OAuth) in
  `~/apps/pwm/backend/src/mcp-server.ts`. Deliberately minimal container environment: it
  only gets `OPTIONGUARD_URL` and `MCP_AUTH_TOKEN`, no DB credentials or Kite secrets at
  all, since it never needs them (previous paragraph).

| Tool | Maps to | Notes |
|---|---|---|
| `check_options(candidates: list[str], thesis: list[str] \| None, source: "kite"\|"manual" = "kite", manual_inputs: list[dict] \| None)` | POST `/checks` | Returns full results. Check `data_source` -- `"mock"` means every number is simulated; the tool always reports this, never silently |
| `get_positions()` | GET `/positions` | Check `has_gtt` per position -- `false` means the stop isn't actually enforced by a real order. Also check `gtt_status` (section 7.8, added) -- `"missing"` or `"triggered"` both mean currently unprotected |
| `get_status()` | GET `/status` | Check `kite_mode` -- `"mock"` means nothing here has touched the real market. Also check `orders.ip_ok` (section 7.8, added) -- `false` means Kite rejected the last order for a static-IP reason |
| `record_trade(check_id, lots, entry_premium, stop_premium?, thesis_stop_level?, time_stop_date?, override_reason?)` | POST `/trades` | Surface `RuleViolation` messages verbatim |
| `tighten_stop(trade_id, ...)` | POST `/trades/{id}/tighten` | Section 7.8: also moves the trade's real GTT if it has one |
| `exit_trade(trade_id, exit_premium, exit_reason)` | POST `/trades/{id}/exit` | |
| `journal_stats(from_date, to_date, group_by)` | GET `/journal/stats` | params renamed from `from`/`to` -- reserved word in Python |
| `list_checks(from_date, to_date, verdict?, traded?)` | GET `/journal/checks` | same rename |
| `add_corporate_event(symbol, event_type, event_date, dividend?)` | POST `/events` | |
| `list_sectors()` | GET `/sectors` | Configured sectors, for building `add_option_thesis`'s sector argument |
| `sync_status()` | GET `/sync/latest` | The last sync run (reconciliation, checks settled, option theses bars/resolve/score/policies). There is no sync-trigger tool -- syncing happens on login or from the web UI only |
| `list_order_intents(status?)` | GET `/orders/intents` | Section 7.8, added, read-only. No tool places, modifies or cancels an order here -- the web UI's order ticket is the only way, ever (7.8.1) |
| `preview_option_thesis(contract, target_premium, stop_premium, duration_days, lots?, source?)` | POST `/option-theses/preview` | Section 7.9, added. Ask the user for target/stop/duration themselves; report the market-implied odds verbatim |
| `add_option_thesis(check_id, why, target_premium, stop_premium, duration_days, lots?, sector?, override_reason?, notes?)` | POST `/option-theses` (`mode` always `"paper"` -- hardcoded, not a parameter) | Section 7.9, added. **Paper only** -- `mode="real"` needs the web UI's Save & buy. A red-verdict check needs `override_reason` |
| `list_option_theses(...)` | GET `/option-theses` | Section 7.9, added |
| `get_option_thesis(thesis_id)` | GET `/option-theses/{id}` | Section 7.9, added |
| `option_thesis_scorecard(...)` | GET `/option-theses/scorecard` | Section 7.9, added, includes the policy comparison block |

Intentionally absent: settings updates, loosening stops, clearing cooling-off, cancelling a
thesis or option thesis, triggering a sync, exiting or linking a trade to an option thesis,
and -- unlike pre-7.8, where *no* order action existed at all -- specifically placing,
modifying or cancelling an order or a GTT. `tighten_stop` is the one MCP tool with any real
Kite-side effect, and only in the risk-reducing direction.

Claude Desktop config example (`claude_desktop_config.json`), stdio variant:

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

HTTP-via-`mcp-remote` variant (needs `mcp` service running: `docker compose up`):

```json
{
  "mcpServers": {
    "optionguard": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:4101/mcp", "--allow-http", "--header", "Authorization:${MCP_AUTH_HEADER}"],
      "env": { "MCP_AUTH_HEADER": "Bearer <same value as MCP_AUTH_TOKEN in .env>" }
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
# Docker only -- same DB, reached by container name instead of 127.0.0.1. See
# "Docker deployment" in section 4.
DATABASE_URL_DOCKER=postgresql+psycopg://youruser:yourpassword@pgdb:5432/optionguard

KITE_API_KEY=
KITE_API_SECRET=
KITE_REDIRECT_URL=http://127.0.0.1:8787/kite/callback

OPTIONGUARD_SECRET_KEY=

# Docker's `mcp` service only (HTTP MCP transport, section 14). openssl rand -hex 32.
MCP_AUTH_TOKEN=
```

`config.toml` (as actually shipped -- `risk_free_rate` and the holiday list are filled
in, not left as placeholders; `[market_data]` is new, see the note below)
```toml
[server]
port = 8787

[pricing]
risk_free_rate = 0.065    # SET to current 91-day T-bill yield, e.g. 0.065
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
# NSE trading holidays, "YYYY-MM-DD", user-maintained
dates = ["2026-01-26", "2026-03-03", "2026-03-26", ...]

[market_data]
# "mock" | "manual_only" | "kite" -- NOT currently wired up anywhere. Added early
# in the build anticipating a config-driven default for /checks' `source` field,
# but that field's actual default ("kite") is hardcoded in api/schemas.py's
# pydantic model instead, and nothing reads this value. Noted here, not fixed,
# since nobody's asked for it -- harmless as-is, just dead config.
default_source = "mock"
```

`config.toml` also has `[sectors]`/`[symbol_sectors]` (used by section 7.9's sector picker),
`[orders]` for section 7.8, and `[option_theses]`/`[option_theses.policies]` for section
7.9 -- see 7.8.2 and 7.9.2 for the full, as-shipped blocks (not repeated here). `[orders].enabled`
stays `false` out of the box.

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
- **Shared sync (`optionguard/sync.py`):** `tests/test_sync.py` -- the advisory-lock 409
  and the login-triggers-sync wiring (generic; each feature's own sync steps are tested
  alongside that feature instead).
- **Option theses (section 7.9, added):** pure-function fixtures in `test_pricing_mc.py`
  (Monte Carlo determinism/sums-to-1/monotonicity), `test_pricing_iv_spot.py`
  (`implied_spot` round-trips), `test_pricing_calendar_hold_cutoff.py` (the 29 Dec → 22 Dec
  example and holiday/weekend snap-back), `test_option_theses_derived.py`,
  `test_option_theses_resolve.py` (clean/gap/ambiguous-bar/slippage fixtures),
  `test_option_theses_scoring.py` and `test_option_theses_policies.py` (the 6-policy exit
  replay fixtures) -- then `test_option_theses_sync.py`, `test_option_theses_scorecard.py`,
  `test_option_theses_order_integration.py` (the `thesis_id` tighten-only guard, OCO,
  link-on-fill), `test_option_theses_api.py`, `test_option_theses_web.py` and
  `test_option_theses_isolation.py` (paper theses never touch monthly P&L/cooloff, mock
  theses never resolved/scored, MCP can't reach exit/cancel/link/resolve) -- see 7.9.11
  for the full checklist these cover.
- **Order ticket (section 7.8, added):** `tests/test_orders_gtt.py` (pure GTT trigger/limit
  math), `test_orders_mock.py` (the mock client's deterministic order/GTT simulation),
  `test_orders_service.py` (guardrails before placement, idempotency, fill handling, GTT
  specifics, exit, reconciliation, the `orders.enabled`/`allow_market` switches, tighten/
  loosen moving the real GTT) and `test_orders_api.py` (MCP/`/api/v1` isolation, the
  IP-error heuristic) -- see 7.8.9 for the full checklist these cover. All run against the
  mock client; nothing here has touched the real Kite order/GTT endpoints (no static IP).

---

## 17. Milestones and acceptance criteria

**M1: Pricing + manual check (CLI/API).** `pricing/`, `analysis/` and the manual provider, plus `POST /checks`, the DB, and logging of checks. The tests in section 16 for pricing, sizing and verdict pass.
✅ Posting `TCS NOV 2500 CE` with manual spot/premium/lot/expiry returns the full metrics and a verdict, and stores a row in `checks`.

**M2: MCP in Claude Desktop.** The `mcp_server` with `check_options`, `get_status` and `add_corporate_event`.
✅ In Claude Desktop, "check TCS NOV 2500 CE, spot 2400, premium 30, lot 175" returns the card data, and Claude explains it from the returned numbers. Works over either entrypoint (section 14) -- stdio or the added HTTP+`mcp-remote` variant.

**M3: Trades + guardrails.** Trades, stop changes and overrides, with all the guardrail tests passing, plus the corresponding MCP tools.
✅ An oversized trade is rejected; a red-verdict trade requires a reason; a tighten that loosens is rejected.

**M4: Kite provider.** Auth flow, instruments cache, quotes, history, chain, OI context and alternatives.
✅ A Kite-mode check needs only the candidate text, and degrades cleanly if historical data isn't available. Built against an offline mock client by default; the real `kiteconnect.KiteConnect` is a credential-only swap (section 8).

**M5: Monitor + Telegram.** ❌ **Not built in v1** -- removed by request; see section 12. Live P&L on the Positions screen is computed on demand instead of via a scheduled poller.

**M6: Web UI.** The four screens; settings loosening is delayed to the next day.
✅ The whole flow (check → take trade → exit → journal) works in the browser. ("Monitor" here means viewing the Positions screen, not a background job -- see M5.)

**M7: Journal stats + settle backfill.**
✅ Stats grouped by verdict show trade counts, win rate and average P&L; skipped checks show their value at expiry.

**M8: Thesis journal.** ❌ **Removed** -- built for an earlier version of this app (a view
on the underlying's price), superseded by section 7.9's option theses (a view on the
option's own premium, scored on a much shorter horizon). See implementation note #6 at
the top of this doc.

**M9: Order ticket (section 7.8, added).**
✅ From a TCS check card, **Buy** opens a ticket for `TCS NOV 2500 CE`: quantity in lots capped at `max_lots`, LIMIT at the ask, NRML, stop at the configured default. Review shows the risk box; Confirm places the order against the mock client.
✅ The fill is recorded at the average price, and a GTT stop at fill price × (1 − stop_pct) is placed.
✅ Tightening the stop on Positions moves the GTT; loosening needs a reason.
✅ When the GTT fires and fills, the next sync (or Positions reconciliation) records the exit as `premium_stop`.
✅ A double-click on Confirm places one order. A trade over `max_lots` never reaches `place_order`.
⚠️ Everything above is verified against the mock client only -- no whitelisted static IP exists yet, so live order placement against the real Kite API is unverified. No time-of-day/market-hours gating, no `egress_proxy` wiring, no untracked-Kite-position notice, and tighten drops an OCO target on rebuild -- see implementation note #7 at the top of this doc for the full list.

**M10: Option theses (section 7.9, added).**
✅ On the Option theses tab, the user enters `TCS DEC 3300 CE` and clicks **Run check**; the check card renders with alternatives. The user sets paper mode, target 85, stop 45, 5 days, 1 lot, sector IT and a reason, and sees reward:risk, ₹ risk vs cap and the market's target-first odds before saving.
✅ A red-verdict contract can't be saved without an override reason; a duplicate-contract save is rejected while the first thesis is open.
✅ On a later day, **Resolve & score** (or the Kite-login sync) fetches minute bars, resolves the thesis as target, stop or time from the bars, and scores it with ₹ P&L, R, MFE/MAE and target-after-stop. The scorecard shows expectancy and `largest_loss_share` for paper theses.
✅ The same thesis appears in the policy table under `plain`, `grace3`, `time_d5`/`d8`/`d11` (where within the duration) and `hold`; `hold` shows as pending until a sync on or after the hold cutoff date (e.g. 22 Dec for a 29 Dec expiry) fetches bars up to that date.
✅ With orders enabled, **Save & buy** on a real thesis opens the order ticket pre-filled with the thesis's lots/stop/target (tighten only, enforced server-side). The fill links to the thesis, updates `entry_premium`, and when the OCO GTT fires the thesis closes as target or stop via the same reconciliation step as 7.8.6.
⚠️ The path chart has no underlying overlay (option close only) -- see implementation note #8 at the top of this doc.

---

## 18. Out of scope / explicit non-goals
- Any directional prediction or "buy" recommendation. Verdicts describe risk and odds only.
- Option selling, except as the covered short leg of a debit spread in v2.
- Multi-user support, cloud hosting, or any exposure beyond 127.0.0.1.
