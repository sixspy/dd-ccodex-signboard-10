"""互斥压测命令 race_eval_no_shadow：同屏求值认账互斥锁压测。

场景：走廊屏仅有开放影子、无正式发布。求值认账路与升正路多线程混抢
锁键「同屏求值认账互斥」（eval_recognize:{screen_id}）。

判据：
1. 全程无 5xx；
2. 升正至多成功一次（无锁双成即失败）；
3. 每个 200 求值：要么空播+拒认痕（升正前），要么认账正式 v1（升正后），绝不认影子；
4. 账实对账：200 求值恰一条认账行，409 求值零行；拒认痕与空播拒认一一对应；
5. 封册：最终求值认账正式 v1，正式区恰一行 v1。

用法：python scripts/race_eval_no_shadow.py  （退出码 0=通过，1=失败）
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="race_eval_no_shadow_"))
db.DATA = TMP
db.DB_PATH = TMP / "app.db"
db.init_db()

from app import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

N_EVAL = 32
N_PROMOTE = 6
FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        FAILURES.append(label)


def login(c: TestClient, username: str, password: str) -> dict:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def main() -> int:
    with TestClient(app) as c:
        h_writer = login(c, "writer", "writer123")
        h_admin = login(c, "admin", "admin123")

        # 复现夹具：走廊屏 scr_corr_c2 仅有开放影子、无正式发布
        screens = c.get("/api/screens", headers=h_writer).json()["items"]
        corr = next(s for s in screens if s["code"] == "scr_corr_c2")
        c.patch(f"/api/screens/{corr['id']}", headers=h_writer, json={"status": "online"})
        playlists = c.get("/api/playlists", headers=h_writer).json()["items"]
        pl = next(p for p in playlists if p["code"] == "pl_lobby_day")
        windows = c.get("/api/windows", headers=h_writer).json()["items"]
        win = next(w for w in windows if w["code"] == "win_workday")
        sh = c.post(
            "/api/publish/shadow",
            headers=h_writer,
            json={
                "screen_id": corr["id"],
                "playlist_id": pl["id"],
                "window_id": win["id"],
                "summary": "走廊日间影子发布（压测）",
            },
        )
        assert sh.status_code == 200, sh.text
        sid = sh.json()["id"]
        print(f"夹具：走廊屏 #{corr['id']} 仅开放影子 #{sid}，无正式发布")

        barrier = threading.Barrier(N_EVAL + N_PROMOTE)

        def do_eval(_: int):
            tc = TestClient(app)
            barrier.wait()
            r = tc.post(
                "/api/evaluate/screen",
                headers=h_writer,
                json={"screen_id": corr["id"], "now_hm": "10:30", "weekday": "1"},
            )
            return r.status_code, r.json()

        def do_promote(_: int):
            tc = TestClient(app)
            barrier.wait()
            r = tc.post(f"/api/publish/shadow/{sid}/promote", headers=h_admin, json={})
            return r.status_code

        print(f"压测：{N_EVAL} 路求值认账 × {N_PROMOTE} 路升正 同抢 eval_recognize:{corr['id']}")
        with ThreadPoolExecutor(max_workers=N_EVAL + N_PROMOTE) as ex:
            eval_futs = [ex.submit(do_eval, i) for i in range(N_EVAL)]
            promote_futs = [ex.submit(do_promote, i) for i in range(N_PROMOTE)]
            evals = [f.result() for f in eval_futs]
            promotes = [f.result() for f in promote_futs]

        eval_codes = [sc for sc, _ in evals]
        check(all(sc in (200, 409) for sc in eval_codes), f"求值无 5xx（分布 200×{eval_codes.count(200)} 409×{eval_codes.count(409)}）")
        promote_ok = promotes.count(200)
        check(all(sc in (200, 409) for sc in promotes), f"升正无 5xx（分布 200×{promote_ok} 409×{promotes.count(409)}）")
        check(promote_ok == 1, f"升正恰成功一次，无锁双成未发生（实际 {promote_ok}）")

        ok_bodies = [b for sc, b in evals if sc == 200]
        bad_recog = [
            b for b in ok_bodies
            if not (
                (b["source"] == "empty" and b.get("recognized") is False and (b.get("rejection") or {}).get("shadow_ids"))
                or (b["source"] == "publication" and b.get("version") == 1 and b.get("recognized") is True)
            )
        ]
        check(not bad_recog, "每个 200 求值：空播拒认 或 认账正式 v1，绝不认影子")

        # 账实对账：认账行 == 200 求值数；拒认痕 == 空播拒认数
        hist = c.get("/api/history", headers=h_admin, params={"limit": 300}).json()["items"]
        rows = [r for r in hist if r["entity_kind"] == "eval_recognition" and r["entity_id"] == corr["id"]]
        check(len(rows) == len(ok_bodies), f"认账行 {len(rows)} == 成功求值 {len(ok_bodies)}（409 零行）")
        audits = c.get("/api/audit/recent", headers=h_admin, params={"limit": 200}).json()["items"]
        traces = [
            a for a in audits
            if a["action"] == "evaluate.reject_shadow" and json.loads(a["payload"]).get("screen_id") == corr["id"]
        ]
        empty_ok = [b for b in ok_bodies if b["source"] == "empty"]
        check(len(traces) == len(empty_ok), f"拒认痕 {len(traces)} == 空播拒认求值 {len(empty_ok)}")

        # 封册：最终求值认账正式 v1，正式区恰一行
        fin = c.post(
            "/api/evaluate/screen",
            headers=h_writer,
            json={"screen_id": corr["id"], "now_hm": "10:30", "weekday": "1"},
        )
        fb = fin.json()
        check(fin.status_code == 200 and fb["source"] == "publication" and fb["version"] == 1, "封册求值认账正式 v1")
        official = c.get("/api/publish/official", headers=h_admin).json()["items"]
        offs = [p for p in official if p["screen_id"] == corr["id"]]
        check(len(offs) == 1 and offs[0]["version"] == 1, "正式区恰一行 v1（账实一致）")
        last = [r for r in hist if r["entity_kind"] == "eval_recognition" and r["entity_id"] == corr["id"]]
        fin2 = c.get("/api/history", headers=h_admin, params={"limit": 300}).json()["items"]
        last = [r for r in fin2 if r["entity_kind"] == "eval_recognition" and r["entity_id"] == corr["id"]]
        check(last and last[0]["action"] == "recognize_official" and "v1" in last[0]["detail"], "最新认账行=recognize_official v1")

    if FAILURES:
        print(f"\nrace_eval_no_shadow：失败 {len(FAILURES)} 项")
        return 1
    print("\nrace_eval_no_shadow：通过（互斥成立，败方零正式改，账实一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
