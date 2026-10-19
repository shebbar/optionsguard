# OptionGuard

A local, single-user tool that evaluates long stock-option trades (NSE, monthly expiry)
**before** entry, enforces personal risk rules, and keeps a journal. Exposed through a
local web UI and an MCP server for Claude Desktop. See [DESIGN.md](DESIGN.md) for the
full spec this implements.

The app does not predict direction. It makes the odds, cost and risk of each trade
explicit and caps position size. **It does not force exits itself** -- v1 has no
background monitor or alerting (see "What's not in v1" below). Place a real GTT stop
order in Kite every time you enter a trade; `GET /positions` warns you if one is missing
for a given position, but that's a read-only check, not enforcement.

v1 places no orders -- you place orders in Kite; the app records positions and lets you
check on them on demand.

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
web UI (Check / Positions / Journal / Settings).

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

The MCP server is **not** part of this compose file -- Claude Desktop spawns it itself as
a local stdio subprocess (see the MCP section below), which isn't a fit for a long-running
container either way. Point it at `http://127.0.0.1:8787`, same URL this publishes.

```bash
docker compose logs -f app     # tail logs
docker compose down            # stop
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
`generate_session`, `get_gtts`), which the mock and the real `KiteConnect` both implement.

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

The MCP server is a thin stdio wrapper over the core's `/api/v1` JSON API -- it never
imports the core or touches the database directly. Start the core service first.

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

## Settling expired checks

`optionguard backfill-settle` computes `expiry_settle` (the option's intrinsic value at
expiry) for any past check that doesn't have one yet, so the Journal's "skipped checks"
view can show what an untraded candidate would have been worth. It only does anything if
you run it -- there's no scheduled job in v1. Run it yourself after market close, or set
up your own cron entry, e.g.:

```cron
30 16 * * 1-5 cd /ABSOLUTE/PATH/TO/optionguard && uv run optionguard backfill-settle
```

(16:30 IST, weekdays -- doesn't account for NSE holidays, but settling a holiday is a
no-op anyway since nothing expires on one.)

## Tests

```bash
uv run pytest
```

Tests run against a throwaway `<your-db>_test` Postgres database (created automatically,
derived from `DATABASE_URL`, never hardcoded). The schema is dropped and recreated fresh
each test session, and tables are truncated between tests.

## What's not in v1

Debit spreads, IV-percentile backfill from NSE bhavcopy, a calibration backtest module,
automated order placement, and WebSocket ticks are all deliberately out of scope for v1 --
see DESIGN.md section 2. The Telegram-alert / scheduled-monitoring loop described in the
original design is also not implemented: the Positions page computes live P&L on demand
per request instead of via a background poller, and there is no alerting subsystem. In its
place, `GET /positions` does a read-only check for a matching GTT order in Kite and warns
if one's missing -- see "It does not force exits itself" above.
