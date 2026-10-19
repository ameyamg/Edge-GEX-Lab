# Contributing to Edge GEX Lab

Thanks for helping. Bug reports, fixes and features are all welcome. This page covers how to set
up, what to test, and what a pull request needs before it can be merged.

**Security problems:** please do not open a public issue. Follow [SECURITY.md](SECURITY.md) and use
GitHub's private vulnerability reporting.

## What the project cares about

These decide most review questions, so they are worth knowing before you start:

1. **Local first.** Everything runs on the user's machine. No accounts, no telemetry, no cloud
   service. A change that sends data somewhere new, opens a network port beyond `127.0.0.1`, or adds
   a background job needs to be discussed in an issue first. Market data stays REST only: no
   websocket (an Alpaca account gets one, and a user's own scanner may hold it) and never an order
   endpoint.
2. **Every number can be traced.** A level on the map comes from the stated GEX formula; a price in
   the trade guidance is a map level or a listed computation of map numbers (`computations` in the
   guidance object). The grounding tests in `tests/test_guidance.py` must keep passing. A change that
   adds a price needs its formula in `computations`.
3. **Label instead of guessing.** When data is old, partial or from a fallback source, the map says
   so (OI date and source, feed, coverage) and the guidance shows no setups. Never hide a gap to make
   the screen look complete.
4. **Small and focused.** One pull request, one change. Large pull requests that bundle several
   features will usually be asked to split.

## Setting up

Requirements: Python 3.11 or newer, Node.js LTS, and Alpaca API keys for SPY, QQQ and stocks (SPX
and NDX work without them).

On Windows, run `setup.bat` once and `launch.bat` to start. By hand:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # Windows: .venv\Scripts\python
cp .env.example .env
cd web && npm ci && npm run build && cd ..
.venv/bin/python -m uvicorn gexlab.api:app --host 127.0.0.1 --port 8240
```

A new install is always empty. For development only, `launch.bat demo` (or `GEXLAB_DEMO=1`) serves
synthetic fixtures from `fixtures/` with no keys and no network, and every screen carries a
"Synthetic fixture" badge. For UI work, run the API on 8240 and `cd web && npm run dev` (Vite on 5240,
proxies `/api`); `npm run dev:mock` runs the UI with no backend at all.
**Never use or commit real account data**: not in tests, fixtures, screenshots or issues.

## Tests

CI runs these on every pull request, and they must pass:

```bash
python -m pytest tests -q
cd web && npm run build
```

What to add:

- **A fix:** a test that fails without it.
- **GEX math, coverage or the valuation clock:** a fixture with hand-worked expected values
  (see `tests/test_core_gex.py` and `tests/test_valuation_clock.py`).
- **Guidance rules:** a test that every new price is a map level or a listed computation, and that
  the rule is silent on a stale or low-coverage map.
- **A UI change:** a screenshot in the pull request, on demo data.

## Adding a data source

Sources live in `gexlab/sources/`. A source returns a `Chain` (`gexlab/models.py`): the spot with its
timestamp, the contracts with open interest, its date and source, and IV with its quote time, plus a
`valuation_ts`. `gexlab/service.py` (`fetch_chain`) routes a ticker to its source, then
`engine/gex.finalize` computes gamma and GEX the same way for every source. Rules for a new source:

- Timestamps are the vendor's, never the request time. Unknown stays unknown and is labeled.
- An IV whose quote time is unknown or stale is dropped in strict mode, never silently used.
- Open interest carries its own date per contract, so old data is visible to the coverage checks.
- Network calls go through the shared rate limiter (`sources/base.py`) with backoff.

## Pull requests

- Branch from `main` and keep the change focused.
- Describe what changed and why, and how you tested it.
- Say so if the change adds a dependency, a network call, file access outside the app folder, or a
  change to the GEX math. New dependencies need a reason.
- Never include `.env` files, API keys, caches or account screenshots.

## Code style

Match the code around you. Comments explain why something is done, not what the next line does.
Every tuning value lives in `gexlab/config.py` with the reason it holds that value. Keep user-facing
text plain and specific: say what happened and what to do next. No em dashes or en dashes anywhere.

## Questions

Ask in [Discussions](https://github.com/simonro/Edge-GEX-Lab/discussions). Never paste API keys or account details.
