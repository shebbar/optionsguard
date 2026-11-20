# OptionGuard

A local, single-user tool that evaluates long stock-option trades (NSE, monthly expiry)
**before** entry, enforces personal risk rules, and keeps a journal. Exposed through a
local web UI and an MCP server for Claude Desktop. See [DESIGN.md](DESIGN.md) for the
full spec this implements.

The app does not predict direction. It makes the odds, cost and risk of each trade
explicit and caps position size. Orders can be placed from the web UI's order ticket
(one at a time, after an explicit Confirm, never from MCP or `/api/v1`) -- every filled
entry gets a GTT stop placed automatically, so a position doesn't sit unprotected. See
"Order ticket" below, including the static-IP caveat: this has only been exercised
against the offline mock so far.

For a trade recorded without going through the ticket (e.g. placed directly in Kite, or
from before this feature existed), **it does not force exits itself** -- `GET /positions`
warns you if a matching GTT order is missing, but that's a read-only check, not
enforcement.

## Setup

Requires Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and a Postgres server/container
you already run locally.

```bash
# one-time: create the database (adjust user/host to your Postgres setup)
createdb optionguard
# or: psql -c "CREATE DATABASE optionguard;"

cp .env.example .env
# edit .env -- at minimum set DATABASE_URL to point at the optionguard database above

uv sync
uv run alembic upgrade head
uv run optionguard serve
```

The core service binds to `127.0.0.1:8787` only. Open `http://127.0.0.1:8787` for the
web UI (Check / Positions / Journal / Theses / Settings).

## Running via Docker Compose

An alternative to the `uv run` setup above -- same app, containerized.

```bash
# one-time, same as above, plus:
createdb optionguard
cp .env.example .env
# edit .env -- fill in DATABASE_URL_DOCKER (see the comment above it in .env.example);
# if your Postgres container isn't on a shared "pwm-net" Docker network already,
# create one and join your Postgres container to it first:
#   docker network create pwm-net
#   docker network connect pwm-net <your-postgres-container-name>

docker compose up --build -d
```

This builds the image, runs `alembic upgrade head` automatically on container start, and
publishes the app on `127.0.0.1:8787` -- same localhost-only guarantee as running it
directly, just reached through Docker's port forwarding instead of uvicorn binding
`127.0.0.1` itself (which wouldn't be reachable at all from outside the container).

This also starts an `mcp` service (Streamable HTTP MCP server on `127.0.0.1:4101`,
bearer-token auth) -- see "MCP server" below for when to use it instead of the stdio
entrypoint, and for the Claude Desktop config it needs.

```bash
docker compose logs -f app     # tail core service logs
docker compose logs -f mcp     # tail MCP server logs
docker compose down            # stop everything
```

## Kite Connect: mock by default

Until you add real Zerodha/Kite API credentials, `source="kite"` checks are served by an
offline mock (`market/mock_kite_client.py`) that mimics the real `kiteconnect.KiteConnect`
client's method signatures and response shapes -- same instrument-dump format, same quote
and historical-data shapes, same login/callback flow (always succeeds). A small fixed
universe of NSE large-caps gets deterministic, seeded random-walk prices so the same day
always produces the same numbers.

To switch to the real API once you have credentials:

1. Set `KITE_API_KEY` and `KITE_API_SECRET` in `.env`.
2. Visit `http://127.0.0.1:8787/kite/login`, log in with your real Zerodha account, and
   let it redirect back to `/kite/callback`. Kite sessions are valid for the current
   trading day only -- you'll need to repeat this each day.
3. `source="kite"` checks now hit the real market. If the session has expired or was
   never established, the API returns `409 KITE_LOGIN_REQUIRED` with a `login_url`
   instead of failing badly.

No code changes are needed for the switch -- `market/kite.py` only depends on the client
method surface (`instruments`, `quote`, `ltp`, `historical_data`, `login_url`,
`generate_session`, `get_gtts`, and -- for the order ticket -- `place_order`,
`order_history`, `modify_order`, `cancel_order`, `place_gtt`, `get_gtt`, `modify_gtt`,
`delete_gtt`), which the mock and the real `KiteConnect` both implement. Order/GTT
placement against the *real* Kite API additionally needs a static IP whitelisted on your
Kite developer profile (since 1 April 2026) -- see "Order ticket" below.

`source="manual"` always works, with no Kite dependency at all -- you supply spot,
premium, lot size and expiry directly per candidate.

**Mock data is tagged, permanently.** A check made with `source="kite"` while no real
credentials exist is stored and returned with `data_source="mock"`, not `"kite"` -- it
stays tagged that way even after you add real credentials, so your journal never mixes
simulated history with real trades under the same label. `GET /status` reports
`kite_mode: "mock"|"live"`, the web UI shows a MOCK DATA banner plus a per-row tag, and
every mock-backed check's `explanation_facts` leads with an explicit warning (so Claude
Desktop sees it too via the MCP server).

## MCP server (Claude Desktop)

Tool definitions live in `mcp_server/server.py` and never import the core or touch the
database directly -- every tool is a thin wrapper over the core's `/api/v1` JSON API, so
both entrypoints below expose the exact same tools. Start the core service first either way.

**Option A -- stdio (simplest, no auth, runs on your machine).** Claude Desktop spawns the
server itself as a local subprocess:

```json
{
  "mcpServers": {
    "optionguard": {
      "command": "uv",
      "args": ["run", "--directory", "/ABSOLUTE/PATH/TO/optionguard", "python", "-m", "mcp_server.server"],
      "env": { "OPTIONGUARD_URL": "http://127.0.0.1:8787" }
    }
  }
}
```

**Option B -- Streamable HTTP in Docker, via `mcp-remote`.** For running the MCP server as
a long-lived container (`docker compose up` starts it as the `mcp` service) instead of a
local subprocess -- useful if you're already running other MCP servers this way. Bearer-token
auth (`MCP_AUTH_TOKEN` in `.env`, `openssl rand -hex 32`) since this one's reachable over the
network rather than spawned directly:

```json
{
  "mcpServers": {
    "optionguard": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://localhost:4101/mcp",
        "--allow-http",
        "--header",
        "Authorization:${MCP_AUTH_HEADER}"
      ],
      "env": { "MCP_AUTH_HEADER": "Bearer <same value as MCP_AUTH_TOKEN in .env>" }
    }
  }
}
```

Add either block's `"optionguard": {...}` entry alongside your other entries under
`mcpServers` in `claude_desktop_config.json` -- don't run both at once under the same name.

## Order ticket

`/order?check_id=…` (opened from a Check card's **Buy** button) or `/order?trade_id=…`
(from a Positions row's **Exit via ticket** button) is the only place an order can be
placed, modified or cancelled -- never from MCP or `/api/v1`. Review runs every guardrail
and shows the risk box; Confirm places the order, and every filled entry gets a GTT stop
placed automatically (OCO with a target, if you set one). `[orders].enabled = false`
(the default) keeps the ticket preview-only -- Review still works, Confirm is refused.

**You'll need a static IP before Confirm can place a real order.** Since 1 April 2026,
Kite rejects order/GTT endpoints from IPs that aren't whitelisted on your developer
profile (quotes and reads still work from anywhere). Everything up to that boundary --
review, confirm, fill polling, GTT math, exit, reconciliation -- is built and tested
against the offline mock client; live placement against the real API is unverified until
a static IP exists. Two ways to get one: an ISP-assigned static IP, or routing *just* the
order calls through a small VPS (`[orders].egress_proxy` in config.toml -- the field
exists but isn't wired up yet; see [DESIGN.md](DESIGN.md) implementation note #7 for this
and the other known gaps against the full spec).

Once a static IP is whitelisted: set `[orders].enabled = true`, log in to Kite, and the
ticket starts placing real orders. `GET /status`'s `orders.ip_ok` turns `false` if Kite
rejects an order for an IP reason, with the message shown verbatim on the status bar.

A ticket opened from an **option thesis** (below) via **Save & buy** pre-fills lots, stop
and target from the thesis -- you can tighten them (smaller size, a stop or target no
further from entry) but never loosen them, enforced server-side regardless of what the
form submits. The fill links back to the thesis and updates its `entry_premium`.

## Option theses

The **Option theses** screen (`/option-theses`) tests a much shorter-horizon idea than the
Theses journal above: an entry, target and stop on the **option contract's own premium**
(not the underlying's price), held for a few trading days, scored from the option's own
minute candles. The shorter horizon builds a usable sample -- 20-30 theses -- in weeks
rather than months. Run a check, set paper or real mode, a target premium, stop premium,
duration and a one-line reason, and see reward:risk, ₹ risk vs the per-trade cap, and the
market's own (risk-neutral, Monte Carlo) odds of reaching the target before the stop, before
saving. **Resolve & score** (folded into the same sync job as the Theses journal) resolves
each open thesis from minute bars as target, stop or a time exit, and replays it under five
alternate exit rules side by side -- a 3-day grace period before the stop applies, three
checkpoint-based time exits, and a "hold until forced out" baseline modelling the old habit
of riding a loser to the week before expiry. The scorecard adds expectancy, payoff ratio
and `largest_loss_share` -- the "one loss wipes out the wins" detector -- plus that same
policy comparison. See [DESIGN.md section 7.9](DESIGN.md#79-option-theses-added-implemented)
for the full spec; its one known gap is that the path chart has no underlying overlay (see
implementation note #8 at the top of DESIGN.md).

MCP can preview and add a **paper** option thesis and read them back, but can never create
a real one or exit, cancel or link one to a trade -- those are web UI only, same isolation
rule as the order ticket.

## Syncing and settling

`optionguard sync` is the one command that keeps everything up to date. It's now 5 steps:
reconcile every open trade's GTT/order state (did a stop fire and fill while you weren't
looking?), fetch each open thesis's option-contract daily bars, score any thesis whose
horizon has passed, settle expired `checks` rows (the original `backfill-settle` behaviour),
and -- for option theses -- fetch minute bars, resolve and score open ones, auto-cancel an
unlinked real thesis past its entry day, and (re)compute all six exit-policy results. It
also runs automatically after every Kite login, and from the **Sync now** / **Resolve &
score** buttons on the Theses / Option theses screens -- so in normal day-to-day use you
don't need to run it by hand at all. It's idempotent and safe to re-run; a second sync while
one is already running gets a clear "already running" response instead of racing it (a
Postgres advisory lock).

`optionguard backfill-settle` still works as a standalone alias for just the settle step,
if you only want that part. Run either manually after market close, or set up your own
cron entry, e.g.:

```cron
30 16 * * 1-5 cd /ABSOLUTE/PATH/TO/optionguard && uv run optionguard sync
```

(16:30 IST, weekdays -- doesn't account for NSE holidays, but syncing on a holiday is a
no-op anyway since nothing moves or expires on one.)

## Tests

```bash
uv run pytest
```

Tests run against a throwaway `<your-db>_test` Postgres database (created automatically,
derived from `DATABASE_URL`, never hardcoded). The schema is dropped and recreated fresh
each test session, and tables are truncated between tests.

## What's not in v1

Debit spreads, IV-percentile backfill from NSE bhavcopy, a calibration backtest module,
automated/scheduled order placement (every order still needs a human to click Confirm),
and WebSocket ticks are all deliberately out of scope for v1 -- see DESIGN.md section 2.
The Telegram-alert / scheduled-monitoring loop described in the original design is also
not implemented: the Positions page computes live P&L on demand per request instead of
via a background poller, and there is no alerting subsystem. In its place, `GET
/positions` does a read-only check for a matching GTT order in Kite and warns if one's
missing -- see "It does not force exits itself" above.

The thesis journal has four known gaps against its own spec -- no live interim progress
on open theses, no live probability text on the new-thesis form, no option axis on the
path chart, and no Sync now button on the Journal screen. See implementation note #6 at
the top of DESIGN.md.

The order ticket has six known gaps against its own spec -- no time-of-day/market-hours
gating, the `egress_proxy` option isn't wired up, no untracked-Kite-position notice during
reconciliation, tightening a GTT-backed trade drops any original OCO target on rebuild, no
dedicated order-call rate limit, and `market_protection` isn't passed through for MARKET
orders. See implementation note #7 at the top of DESIGN.md. Most importantly: it has only
been exercised against the offline mock client -- there's no whitelisted static IP yet, so
live order placement against the real Kite API is unverified.

Option theses have one known gap against their own spec -- the path chart has no
underlying overlay (option close only; there's no underlying minute-bar table in this
build). See implementation note #8 at the top of DESIGN.md.
