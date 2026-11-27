"""失败测甲：仅有开放影子、无正式发布时求值。

正确性口径（甲路径）：求值只认正式已上屏发布；仅影子时正式求值结果为空播，
并记拒认痕；正式结果册（change_history: eval_recognition）不得写入清单行。
升正之后才准吐清单行。
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
    return corr, pl, win, r.json()["id"]


def _eval_rows(client, h, screen_id):
    items = client.get("/api/history", headers=h, params={"limit": 300}).json()["items"]
    return [r for r in items if r["entity_kind"] == "eval_recognition" and r["entity_id"] == screen_id]


def _reject_traces(client, h, screen_id):
    items = client.get("/api/audit/recent", headers=h, params={"limit": 200}).json()["items"]
    return [r for r in items if r["action"] == "evaluate.reject_shadow" and str(screen_id) in r["payload"]]


def test_shadow_only_evaluate_empty_broadcast_with_rejection_trace(client):
    """仅影子时：求值空播、不认账清单、记拒认痕；正式结果册无清单行。"""
    h = _login(client)
    corr, pl, win, sid = _corridor_shadow(client, h)

    ev = client.post(
        "/api/evaluate/screen",
        headers=h,
        json={"screen_id": corr["id"], "now_hm": "10:30", "weekday": "1"},
    )
    assert ev.status_code == 200, ev.text
    body = ev.json()
    # 空播：不得把开放影子当生效
    assert body["source"] == "empty", body
    assert body["play"] is None, body
    assert body.get("recognized") is False, body
    # 拒认痕随响应回执
    rejection = body.get("rejection") or {}
    assert sid in (rejection.get("shadow_ids") or []), body

    # 正式结果册：只有空播认账行，绝无清单行
    rows = _eval_rows(client, h, corr["id"])
    assert len(rows) == 1, rows
    assert rows[0]["action"] == "empty_broadcast", rows
    assert "清单行" not in rows[0]["detail"], rows
    # 拒认痕落审计
    traces = _reject_traces(client, h, corr["id"])
    assert len(traces) == 1, traces


def test_list_rows_only_after_promote(client):
    """升正前求值不认清单行；升正后才吐清单行并认账正式版本。"""
    h = _login(client)
    corr, pl, win, sid = _corridor_shadow(client, h)

    # 升正前：空播，无清单行认账
    ev0 = client.post(
        "/api/evaluate/screen",
        headers=h,
        json={"screen_id": corr["id"], "now_hm": "10:30", "weekday": "1"},
    ).json()
    assert ev0["source"] == "empty"
    assert not [r for r in _eval_rows(client, h, corr["id"]) if r["action"] == "recognize_official"]

    pr = client.post(f"/api/publish/shadow/{sid}/promote", headers=h, json={})
    assert pr.status_code == 200, pr.text
    assert pr.json()["version"] == 1

    # 升正后：吐清单行并认账正式 v1
    ev1 = client.post(
        "/api/evaluate/screen",
        headers=h,
        json={"screen_id": corr["id"], "now_hm": "10:30", "weekday": "1"},
    )
    assert ev1.status_code == 200, ev1.text
    body = ev1.json()
    assert body["source"] == "publication", body
    assert body["version"] == 1, body
    assert body.get("recognized") is True, body
    items = (body["play"] or {}).get("items") or []
    assert len(items) == 3, body  # pl_lobby_day 三条清单行

    rows = _eval_rows(client, h, corr["id"])
    official_rows = [r for r in rows if r["action"] == "recognize_official"]
    assert len(official_rows) == 1, rows
    assert "v1" in official_rows[0]["detail"], official_rows
    assert "清单行 3 条" in official_rows[0]["detail"], official_rows
