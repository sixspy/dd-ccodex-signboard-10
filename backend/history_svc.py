"""变更历史。"""
from __future__ import annotations

from fastapi import APIRouter, Header

from auth import current_user
from db import connect, rows_to_list

router = APIRouter(prefix="/api/history", tags=["history"])

ENTITY_LABEL = {
    "eval_recognition": "求值认账",
    "publication": "正式发布",
    "publish_shadow": "影子发布",
    "panic": "紧急撤屏",
    "panic_shadow": "影子撤屏",
    "override": "单屏覆盖",
    "screen": "屏幕台账",
}
ACTION_LABEL = {
    "recognize_official": "认账正式发布",
    "recognize_override": "认账单屏覆盖",
    "empty_broadcast": "空播",
}


@router.get("")
def list_history(limit: int = 100, authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = rows_to_list(
            conn.execute(
                "SELECT * FROM change_history ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 300)),),
            ).fetchall()
        )
    for r in rows:
        r["entity_label"] = ENTITY_LABEL.get(r["entity_kind"], r["entity_kind"])
        r["action_label"] = ACTION_LABEL.get(r["action"], r["action"])
    return {"items": rows}
