"""
SNS 예약 발행: 저장 + 주기적으로 시간 된 항목 자동 발행.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
SNS_DATA_DIR = ROOT / "data"
SNS_SCHEDULE_FILE = SNS_DATA_DIR / "sns_schedule.json"


def _load_schedule() -> List[Dict[str, Any]]:
    if not SNS_SCHEDULE_FILE.exists():
        return []
    try:
        data = json.loads(SNS_SCHEDULE_FILE.read_text(encoding="utf-8"))
        return data.get("items", [])
    except Exception:
        return []


def _save_schedule(items: List[Dict[str, Any]]) -> None:
    SNS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNS_SCHEDULE_FILE.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def list_scheduled(include_posted: bool = False) -> List[Dict[str, Any]]:
    items = _load_schedule()
    if not include_posted:
        items = [x for x in items if x.get("status") == "pending"]
    return items


def add_scheduled(
    connection_id: str,
    caption: str,
    scheduled_at: str,
    image_url: Optional[str] = None,
    video_url: Optional[str] = None,
    idea: Optional[str] = None,
) -> Dict[str, Any]:
    """scheduled_at: ISO datetime string (e.g. 2025-02-02T14:00:00)."""
    items = _load_schedule()
    item_id = str(int(time.time() * 1000)) + "_" + str(len(items))
    item = {
        "id": item_id,
        "connection_id": connection_id,
        "caption": caption,
        "image_url": image_url,
        "video_url": video_url,
        "idea": idea,
        "scheduled_at": scheduled_at,
        "status": "pending",
        "created_at": time.time(),
        "posted_at": None,
        "error": None,
    }
    items.append(item)
    _save_schedule(items)
    return item


def get_due_items() -> List[Dict[str, Any]]:
    now = time.time()
    items = _load_schedule()
    due = []
    for x in items:
        if x.get("status") != "pending":
            continue
        at = x.get("scheduled_at")
        if not at:
            continue
        try:
            # ISO "2025-02-02T14:00:00" or "2025-02-02T14:00:00.000Z"
            from datetime import datetime
            if "T" in at:
                dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(at, "%Y-%m-%d %H:%M:%S")
            ts = dt.timestamp()
            if ts <= now:
                due.append(x)
        except Exception:
            pass
    return due


def mark_posted(item_id: str, post_id: Optional[str] = None) -> bool:
    items = _load_schedule()
    for x in items:
        if x.get("id") == item_id:
            x["status"] = "posted"
            x["posted_at"] = time.time()
            if post_id:
                x["post_id"] = post_id
            _save_schedule(items)
            return True
    return False


def mark_failed(item_id: str, error: str) -> bool:
    items = _load_schedule()
    for x in items:
        if x.get("id") == item_id:
            x["status"] = "failed"
            x["error"] = error
            x["posted_at"] = time.time()
            _save_schedule(items)
            return True
    return False


def delete_scheduled(item_id: str) -> bool:
    items = _load_schedule()
    before = len(items)
    items = [x for x in items if x.get("id") != item_id]
    if len(items) < before:
        _save_schedule(items)
        return True
    return False
