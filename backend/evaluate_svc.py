"""在线求值：某屏此刻应播什么。

正确性口径（甲路径）：求值只认正式已上屏发布，开放影子一律拒认。
每次求值即一次认账：同屏求值认账互斥锁内，单事务联查正式发布并落正式结果册
（change_history: eval_recognition），仅影子时正式求值结果为空播并记拒认痕
（audit_log: evaluate.reject_shadow）。锁争用败方 409，零正式改。
"""
from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, Header, HTTPException

from audit_svc import write_audit_tx, write_history_tx
from auth import current_user
from db import connect, rows_to_list
from locks import acquire, release
from validators import require_hm, require_positive_int, window_covers

router = APIRouter(prefix="/api/evaluate", tags=["evaluate"])

EVAL_RECOGNITION_KIND = "eval_recognition"
REJECT_SHADOW_ACTION = "evaluate.reject_shadow"


def eval_recognize_lock_key(screen_id: int) -> str:
    """锁键「同屏求值认账互斥」：求值认账与发布/升正同握此键。"""
    return f"eval_recognize:{screen_id}"


@router.post("/screen")
def evaluate_screen(body: dict, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    screen_id = require_positive_int(body.get("screen_id"), "屏幕编号")
    now_hm = require_hm(body.get("now_hm") or datetime.now().strftime("%H:%M"), "求值时刻")
    weekday = str(body.get("weekday") or datetime.now().isoweekday())
    lock_key = eval_recognize_lock_key(screen_id)
    acquire(lock_key, user["username"])
    try:
        with connect() as conn:
            # 同事务跨表账实：联查与落册必须同一快照，BEGIN IMMEDIATE 防语句级脏读
            conn.execute("BEGIN IMMEDIATE")
            screen = conn.execute("SELECT * FROM screens WHERE id=?", (screen_id,)).fetchone()
            if not screen:
                raise HTTPException(404, "屏幕不存在")
            # 联查正式发布与开放影子：认账落册必须与联查同事务，跨表账实一致
            ov = conn.execute(
                "SELECT * FROM overrides WHERE screen_id=? AND official=1 AND active=1 ORDER BY id DESC LIMIT 1",
                (screen_id,),
            ).fetchone()
            pub = conn.execute(
                "SELECT * FROM publications WHERE screen_id=? AND official=1 AND status='published'",
                (screen_id,),
            ).fetchone()
            open_shadows = rows_to_list(
                conn.execute(
                    "SELECT id FROM publish_shadows WHERE screen_id=? AND status='open' ORDER BY id",
                    (screen_id,),
                ).fetchall()
            )
            shadow_ids = [s["id"] for s in open_shadows]

            if ov:
                result = {
                    "source": "override",
                    "effect": ov["effect"],
                    "playlist_id": ov["playlist_id"],
                    "asset_id": ov["asset_id"],
                    "now_hm": now_hm,
                    "recognized": True,
                    "rejection": None,
                }
                if ov["effect"] == "force_blank":
                    result["play"] = None
                elif ov["effect"] == "force_asset" and ov["asset_id"]:
                    asset = conn.execute("SELECT * FROM assets WHERE id=?", (ov["asset_id"],)).fetchone()
                    result["play"] = {"kind": "asset", "asset": dict(asset) if asset else None}
                elif ov["effect"] == "force_playlist" and ov["playlist_id"]:
                    items = rows_to_list(
                        conn.execute(
                            "SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY seq",
                            (ov["playlist_id"],),
                        ).fetchall()
                    )
                    result["play"] = {"kind": "playlist", "items": items}
                result["ledger_id"] = write_history_tx(
                    conn,
                    EVAL_RECOGNITION_KIND,
                    screen_id,
                    "recognize_override",
                    f"认账单屏覆盖 {ov['effect']}",
                    user["username"],
                )
                conn.commit()
                return result

            if not pub:
                # 仅影子（或空屏）：正式求值结果为空播；开放影子一律拒认并记拒认痕
                if shadow_ids:
                    reason = "无正式发布（拒认未升正影子）"
                    detail = "空播：拒认未升正影子 " + " ".join(f"#{sid}" for sid in shadow_ids)
                else:
                    reason = "无正式发布"
                    detail = "空播：无正式发布"
                ledger_id = write_history_tx(
                    conn, EVAL_RECOGNITION_KIND, screen_id, "empty_broadcast", detail, user["username"]
                )
                if shadow_ids:
                    write_audit_tx(
                        conn,
                        user["username"],
                        REJECT_SHADOW_ACTION,
                        {"screen_id": screen_id, "shadow_ids": shadow_ids, "now_hm": now_hm},
                    )
                conn.commit()
                return {
                    "source": "empty",
                    "play": None,
                    "now_hm": now_hm,
                    "reason": reason,
                    "recognized": False,
                    "ledger_id": ledger_id,
                    "rejection": (
                        {"reason": "拒认未升正影子发布", "shadow_ids": shadow_ids} if shadow_ids else None
                    ),
                }

            window = conn.execute(
                "SELECT * FROM time_windows WHERE id=?",
                (pub["window_id"],),
            ).fetchone()
            if weekday not in set((window["weekdays"] or "").split(",")):
                ledger_id = write_history_tx(
                    conn,
                    EVAL_RECOGNITION_KIND,
                    screen_id,
                    "recognize_official",
                    f"认账正式发布 v{pub['version']}（时段外空播：非开放星期）",
                    user["username"],
                )
                conn.commit()
                return {
                    "source": "publication",
                    "play": None,
                    "reason": "非开放星期",
                    "now_hm": now_hm,
                    "recognized": True,
                    "ledger_id": ledger_id,
                    "rejection": None,
                    "version": pub["version"],
                }
            if not window_covers(window["start_hm"], window["end_hm"], now_hm):
                ledger_id = write_history_tx(
                    conn,
                    EVAL_RECOGNITION_KIND,
                    screen_id,
                    "recognize_official",
                    f"认账正式发布 v{pub['version']}（时段外空播：不在时段窗内）",
                    user["username"],
                )
                conn.commit()
                return {
                    "source": "publication",
                    "play": None,
                    "reason": "不在时段窗内",
                    "now_hm": now_hm,
                    "recognized": True,
                    "ledger_id": ledger_id,
                    "rejection": None,
                    "version": pub["version"],
                }
            items = rows_to_list(
                conn.execute(
                    """
                    SELECT i.*, a.code asset_code, a.title asset_title, a.kind
                    FROM playlist_items i JOIN assets a ON a.id=i.asset_id
                    WHERE i.playlist_id=? ORDER BY i.seq
                    """,
                    (pub["playlist_id"],),
                ).fetchall()
            )
            ledger_id = write_history_tx(
                conn,
                EVAL_RECOGNITION_KIND,
                screen_id,
                "recognize_official",
                f"认账正式发布 v{pub['version']}（清单行 {len(items)} 条）",
                user["username"],
            )
            conn.commit()
            return {
                "source": "publication",
                "version": pub["version"],
                "playlist_id": pub["playlist_id"],
                "window_id": pub["window_id"],
                "play": {"kind": "playlist", "items": items},
                "now_hm": now_hm,
                "recognized": True,
                "ledger_id": ledger_id,
                "rejection": None,
            }
    finally:
        release(lock_key, user["username"])
