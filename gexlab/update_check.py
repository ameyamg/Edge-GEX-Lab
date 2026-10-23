"""Is there a newer Edge GEX Lab release on GitHub? One anonymous request, at most once a day.

Someone who downloaded a release zip has no other way to hear about the next one: GitHub only notifies people who
watch the repo. When the API starts it asks GitHub's public latest-release endpoint on a background thread, keeps
the answer in data/cache/update_check.json for a day, prints one console line when a newer version exists, and
serves the answer at GET /api/update for the banner on the page.

What goes out: one GET to api.github.com. GitHub sees your IP address and the app version in the User-Agent,
nothing else: no keys, no tickers, no settings. Any error (offline, rate limited, GitHub down) is silent and is not
retried until the day is up. GEXLAB_NO_UPDATE_CHECK=1 turns it off, and demo mode never checks (demo promises no
network). It never downloads or installs anything: the banner links to the release notes and their update steps.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from gexlab import config

log = logging.getLogger("gexlab.update")

RELEASES_PAGE = f"https://github.com/{config.UPDATE_REPO}/releases"
_API = f"https://api.github.com/repos/{config.UPDATE_REPO}/releases/latest"
_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+[0-9A-Za-z.]{0,20}$")
_MAX_BYTES = 256 * 1024                   # a release JSON is a few KB
_lock = threading.Lock()


def parse_version(v: str | None) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3). Anything unparsable is (0,), older than every release."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else (0,)


def enabled() -> bool:
    off = os.getenv("GEXLAB_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes", "on")
    return not off and not config.is_demo()


def _clean(text, limit: int) -> str:
    """Printable characters only (nothing a terminal would interpret), capped."""
    return "".join(ch for ch in str(text or "") if ch.isprintable())[:limit]


def _fetch() -> dict:
    req = urllib.request.Request(_API, headers={"Accept": "application/vnd.github+json",
                                                "User-Agent": f"edge-gex-lab/{config.APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=config.UPDATE_TIMEOUT_S) as r:
        return json.loads(r.read(_MAX_BYTES + 1)[:_MAX_BYTES].decode("utf-8"))


def _cache_path() -> Path:
    return config.CACHE_DIR / "update_check.json"


def _read_cache(path: Path) -> dict | None:
    try:
        c = json.loads(path.read_text(encoding="utf-8"))
        return c if isinstance(c, dict) and isinstance(c.get("checked_at"), str) else None
    except (OSError, ValueError):
        return None


def _result(latest: str | None, url: str, title: str, checked_at: str | None, error: str | None) -> dict:
    current = config.APP_VERSION
    return {"enabled": True, "current": current, "latest": latest,
            "available": latest is not None and parse_version(latest) > parse_version(current),
            "url": url, "title": title, "checked_at": checked_at, "error": error}


def check(now: datetime | None = None, fetch: Callable[[], dict] | None = None,
          cache_path: Path | None = None) -> dict:
    """The latest release, from the day-old cache or one request to GitHub. Never raises."""
    if not enabled():
        return {"enabled": False, "current": config.APP_VERSION, "latest": None, "available": False,
                "url": RELEASES_PAGE, "title": "", "checked_at": None, "error": None}
    now = now or datetime.now(timezone.utc)
    path = cache_path or _cache_path()
    with _lock:
        c = _read_cache(path)
        if c is not None:
            try:
                age = now - datetime.fromisoformat(c["checked_at"])
            except ValueError:
                age = None
            if age is not None and timedelta(0) <= age < timedelta(hours=config.UPDATE_EVERY_H):
                return _result(c.get("latest"), c.get("url") or RELEASES_PAGE, c.get("title") or "",
                               c["checked_at"], c.get("error"))
        latest, url, title, error = None, RELEASES_PAGE, "", None
        try:
            data = (fetch or _fetch)()
            if not isinstance(data, dict):
                raise ValueError("unexpected release payload")
            tag = str(data.get("tag_name") or "")
            latest = tag if _TAG_RE.match(tag) else None       # only a well-formed version tag counts
            u = str(data.get("html_url") or "")
            # The link only ever points at this repo's releases, whatever the response says.
            url = u if u.startswith(RELEASES_PAGE + "/") and _clean(u, 300) == u else RELEASES_PAGE
            title = _clean(data.get("name"), 120)
        except Exception as e:                                 # offline, rate limited, GitHub down: all fine
            error = _clean(e, 200)
            log.debug("update check failed: %s", e)
        checked_at = now.isoformat()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"checked_at": checked_at, "latest": latest, "url": url, "title": title,
                                        "error": error}), encoding="utf-8")
        except OSError:
            pass                                               # read-only folder: check again next start
        return _result(latest, url, title, checked_at, error)


def console_line(r: dict) -> str | None:
    """The one line printed in the launch.bat window when a newer release exists."""
    if not r.get("available"):
        return None
    return (f"A newer Edge GEX Lab is out: {r['latest']} (you run v{r['current']}). Release notes and update steps: "
            f"{r['url']}")


def start_background_check() -> None:
    """On API start: check on a daemon thread and print one line when an update exists. No-op when disabled."""
    if not enabled():
        return

    def run():
        line = console_line(check())
        if line:
            print(f"\n  {line}\n", flush=True)
    threading.Thread(target=run, daemon=True, name="update-check").start()
