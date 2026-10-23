"""Startup update check: newer, same and older releases, the day-long cache, silence on errors, the opt-outs, and
the guards on what GitHub's answer may put on screen. No network: every fetch is injected."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gexlab import config, update_check as U

NOW = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
PAGE = "https://github.com/simonro/Edge-GEX-Lab/releases"


@pytest.fixture
def on(monkeypatch, tmp_path):
    """The check enabled (the shared conftest turns it off), at version 1.1.0, with a temp cache file."""
    monkeypatch.delenv("GEXLAB_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(config, "is_demo", lambda: False)
    monkeypatch.setattr(config, "APP_VERSION", "1.1.0")
    return tmp_path / "update_check.json"


def release(tag, url=None, name="Edge GEX Lab"):
    calls = []

    def fetch():
        calls.append(1)
        return {"tag_name": tag, "html_url": url or f"{PAGE}/tag/{tag}", "name": name}
    return fetch, calls


@pytest.mark.parametrize("tag, available", [("v1.2.0", True), ("v1.1.0", False), ("v1.0.2", False),
                                            ("v2.0.0", True), ("v1.1.1", True)])
def test_newer_same_and_older(on, tag, available):
    fetch, _ = release(tag)
    r = U.check(NOW, fetch, on)
    assert r["available"] is available and r["latest"] == tag and r["current"] == "1.1.0"
    assert (U.console_line(r) is not None) is available


def test_cache_answers_for_a_day_then_checks_again(on):
    fetch, calls = release("v1.2.0")
    U.check(NOW, fetch, on)
    r = U.check(NOW + timedelta(hours=23), fetch, on)
    assert len(calls) == 1 and r["available"] and r["checked_at"] == NOW.isoformat()
    U.check(NOW + timedelta(hours=24, minutes=1), fetch, on)
    assert len(calls) == 2


def test_network_error_is_silent_and_waits_a_day(on):
    def offline():
        raise OSError("no route to host")
    r = U.check(NOW, offline, on)
    assert r["available"] is False and r["latest"] is None and "no route" in r["error"]
    fetch, calls = release("v1.2.0")
    assert U.check(NOW + timedelta(hours=2), fetch, on)["available"] is False and calls == []   # at most once a day


def test_a_newer_install_compares_with_its_own_version(on, monkeypatch):
    U.check(NOW, release("v1.2.0")[0], on)
    monkeypatch.setattr(config, "APP_VERSION", "1.2.0")       # updated since the cached check
    assert U.check(NOW + timedelta(hours=1), release("v1.2.0")[0], on)["available"] is False


def test_opt_out_and_demo_never_fetch(on, monkeypatch):
    def boom():
        raise AssertionError("must not fetch")
    monkeypatch.setenv("GEXLAB_NO_UPDATE_CHECK", "1")
    r = U.check(NOW, boom, on)
    assert r["enabled"] is False and r["available"] is False and r["current"] == "1.1.0"
    monkeypatch.delenv("GEXLAB_NO_UPDATE_CHECK")
    monkeypatch.setattr(config, "is_demo", lambda: True)
    assert U.check(NOW, boom, on)["enabled"] is False
    assert not on.exists()


def test_only_a_version_tag_and_this_repos_link_reach_the_screen(on):
    r = U.check(NOW, release("latest-build")[0], on)
    assert r["latest"] is None and r["available"] is False
    on.unlink()
    r = U.check(NOW, release("v1.2.0", url="https://evil.example/download", name="x\x1b[2Jy")[0], on)
    assert r["url"] == PAGE and r["title"] == "x[2Jy"
    on.unlink()
    r = U.check(NOW, lambda: ["not", "a", "dict"], on)
    assert r["latest"] is None and r["error"]


def test_a_corrupt_cache_is_ignored(on):
    on.write_text("{not json", encoding="utf-8")
    fetch, calls = release("v1.2.0")
    assert U.check(NOW, fetch, on)["available"] and len(calls) == 1


def test_api_route(on, monkeypatch):
    from gexlab import api
    monkeypatch.setattr(U, "_cache_path", lambda: on)
    monkeypatch.setattr(U, "_fetch", release("v1.3.0")[0])
    body = TestClient(api.app, base_url="http://127.0.0.1").get("/api/update").json()
    assert body["available"] is True and body["latest"] == "v1.3.0" and body["url"] == f"{PAGE}/tag/v1.3.0"
