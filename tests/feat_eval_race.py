"""争用失败测：两路抢「同屏求值认账互斥」（eval_recognize:{screen_id}）。

无锁双成即事故：求值认账路与发布/升正路必须同握一把互斥锁，
败方 409 且零正式改（求值败方零认账行，升正败方零正式区改动）。
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PYTHONPATH", str(ROOT / "backend"))

from db import init_db  # noqa: E402
from app import app  # noqa: E402

LOCK_KEY = "eval_recognize:{screen_id}"  # 同屏求值认账互斥


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


def _eval_rows(client, h, screen_id):
    items = client.get("/api/history", headers=h, params={"limit": 300}).json()["items"]
    return [r for r in items if r["entity_kind"] == "eval_recognition" and r["entity_id"] == screen_id]


def test_mutex_held_blocks_both_paths_loser_zero_official_change(client):
    """锁被持有时两路都必败：求值 409 零认账行，升正 409 正式区零改。"""
    h_writer = _login(client)
    h_admin = _login(client, "admin", "admin123")
    corr, sid = _corridor_shadow(client, h_writer)
    lock_key = LOCK_KEY.format(screen_id=corr["id"])

    hold = client.post("/api/locks/acquire", headers=h_admin, json={"lock_key": lock_key})
    assert hold.status_code == 200, hold.text
    try:
        # 求值路：必 409，且零认账行（败方零正式改）
        ev = client.post(
            "/api/evaluate/screen",
            headers=h_writer,
            json={"screen_id": corr["id"], "now_hm": "10:30", "weekday": "1"},
        )
        assert ev.status_code == 409, ev.text
        assert _eval_rows(client, h_admin, corr["id"]) == []

        # 升正路：必 409，正式区仍空、影子仍 open（败方零正式改）
        pr = client.post(f"/api/publish/shadow/{sid}/promote", headers=h_writer, json={})
        assert pr.status_code == 409, pr.text
        official = client.get("/api/publish/official", headers=h_admin).json()["items"]
        assert not [p for p in official if p["screen_id"] == corr["id"]], official
        shadows = client.get("/api/publish/shadows", headers=h_admin).json()["items"]
        assert next(s for s in shadows if s["id"] == sid)["status"] == "open"
    finally:
        client.post("/api/locks/release", headers=h_admin, json={"lock_key": lock_key})


def test_concurrent_recognize_and_promote_serialize(client):
    """多线程混抢：无 5xx；每个 200 求值恰一条认账行；409 零行；最终账实一致。"""
    h_writer = _login(client)
    h_admin = _login(client, "admin", "admin123")
    corr, sid = _corridor_shadow(client, h_writer)
    n_eval = 8
    barrier = threading.Barrier(n_eval + 1)

    def do_eval():
        c = TestClient(app)
        barrier.wait()
        r = c.post(
            "/api/evaluate/screen",
            headers=h_writer,
            json={"screen_id": corr["id"], "now_hm": "10:30", "weekday": "1"},
        )
        return r.status_code, r.json()

    def do_promote():
        c = TestClient(app)
        barrier.wait()
        r = c.post(f"/api/publish/shadow/{sid}/promote", headers=h_admin, json={})
        return r.status_code

    with ThreadPoolExecutor(max_workers=n_eval + 1) as ex:
        eval_futs = [ex.submit(do_eval) for _ in range(n_eval)]
        promote_fut = ex.submit(do_promote)
        eval_results = [f.result() for f in eval_futs]
        promote_status = promote_fut.result()

    # 无 5xx；求值只有 200/409 两态
    assert all(sc in (200, 409) for sc, _ in eval_results), eval_results
    assert promote_status in (200, 409), promote_status
    ok_evals = [body for sc, body in eval_results if sc == 200]
    conflict_evals = [body for sc, body in eval_results if sc == 409]

    # 每个成功求值：要么空播拒认（升正前），要么认账正式 v1（升正后），绝不认影子
    for body in ok_evals:
        if body["source"] == "empty":
            assert body.get("recognized") is False, body
            assert (body.get("rejection") or {}).get("shadow_ids"), body
        else:
            assert body["source"] == "publication", body
            assert body["version"] == 1, body
            assert body.get("recognized") is True, body

    # 账实对账：200 求值恰一行认账，409 求值零行
    rows = _eval_rows(client, h_admin, corr["id"])
    assert len(rows) == len(ok_evals), (len(rows), len(ok_evals), len(conflict_evals))

    # 收尾封册：若升正被挤败则补升正，最终求值必认正式 v1
    if promote_status != 200:
        pr = client.post(f"/api/publish/shadow/{sid}/promote", headers=h_admin, json={})
        assert pr.status_code == 200, pr.text
    final = client.post(
        "/api/evaluate/screen",
        headers=h_writer,
        json={"screen_id": corr["id"], "now_hm": "10:30", "weekday": "1"},
    )
    assert final.status_code == 200, final.text
    body = final.json()
    assert body["source"] == "publication" and body["version"] == 1, body
    last_rows = _eval_rows(client, h_admin, corr["id"])
    assert last_rows[0]["action"] == "recognize_official", last_rows
    assert "v1" in last_rows[0]["detail"], last_rows
