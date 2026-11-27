"""失败测乙：发布页/屏幕台账不得把开放影子当成正式已上屏（假绿）。

正式区空白就是空白；影子行必须自带「未上屏」防伪字段；
屏幕台账的上屏真相只联查正式发布；总览计数不得把影子计入正式。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PYTHONPATH", str(ROOT / "backend"))

from db import init_db  # noqa: E402
from app import app  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    monkeypatch.setattr("db.DB_PATH", db)
    monkeypatch.setattr("db.DATA", tmp_path)
    if db.exists():
        db.unlink()
    init_db()
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _login(client, username="writer", password="writer123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _corridor_shadow(client, h):
    """复现夹具：走廊屏 scr_corr_c2 仅有开放影子、无正式发布。"""
    screens = client.get("/api/screens", headers=h).json()["items"]
    corr = next(s for s in screens if s["code"] == "scr_corr_c2")
    client.patch(f"/api/screens/{corr['id']}", headers=h, json={"status": "online"})
    playlists = client.get("/api/playlists", headers=h).json()["items"]
    pl = next(p for p in playlists if p["code"] == "pl_lobby_day")
    windows = client.get("/api/windows", headers=h).json()["items"]
    win = next(w for w in windows if w["code"] == "win_workday")
    r = client.post(
        "/api/publish/shadow",
        headers=h,
        json={
            "screen_id": corr["id"],
            "playlist_id": pl["id"],
            "window_id": win["id"],
            "summary": "走廊日间影子发布",
        },
    )
    assert r.status_code == 200, r.text
    return corr, r.json()["id"]


def test_shadow_never_listed_as_official(client):
    """正式区空白；影子行自带 official=0 / on_screen=False / 待升正 防伪标记。"""
    h = _login(client)
    corr, sid = _corridor_shadow(client, h)

    official = client.get("/api/publish/official", headers=h).json()["items"]
    assert not [p for p in official if p["screen_id"] == corr["id"]], official

    shadows = client.get("/api/publish/shadows", headers=h).json()["items"]
    row = next(s for s in shadows if s["id"] == sid)
    assert row["status"] == "open", row
    assert row.get("official") == 0, row  # 影子永不自认正式
    assert row.get("on_screen") is False, row  # 影子永不自认已上屏
    assert row.get("status_label") == "待升正", row  # 不得亮「已上屏」假绿


def test_screen_on_screen_truth_comes_from_official_only(client):
    """屏幕台账上屏真相只联查正式发布；升正后才翻绿。"""
    h = _login(client)
    corr, sid = _corridor_shadow(client, h)

    def row():
        items = client.get("/api/screens", headers=h).json()["items"]
        return next(s for s in items if s["id"] == corr["id"])

    r0 = row()
    assert r0.get("on_screen") is False, r0
    assert r0.get("official_version") is None, r0
    assert r0.get("open_shadows") == 1, r0

    pr = client.post(f"/api/publish/shadow/{sid}/promote", headers=h, json={})
    assert pr.status_code == 200, pr.text

    r1 = row()
    assert r1.get("on_screen") is True, r1
    assert r1.get("official_version") == 1, r1
    assert r1.get("open_shadows") == 0, r1


def test_dashboard_never_counts_shadow_as_official(client):
    """总览：影子不进正式发布计数，只进待升正影子计数。"""
    h = _login(client)
    before = client.get("/api/dashboard", headers=h).json()
    corr, sid = _corridor_shadow(client, h)
    after = client.get("/api/dashboard", headers=h).json()
    assert after["official_publications"] == before["official_publications"], after
    assert after["open_shadows"] == before["open_shadows"] + 1, after
