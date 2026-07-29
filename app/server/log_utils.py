"""日志模块：记录、查询、统计"""

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

LOG_LEVELS = {"debug", "info", "warning", "error"}
log_file_lock = threading.Lock()

# 在初始化时由 backend 设置
LOG_FILE: Optional[Path] = None
STATE_DIR: Optional[Path] = None


def init(file_path: Path, state_dir: Path) -> None:
    global LOG_FILE, STATE_DIR
    LOG_FILE = file_path
    STATE_DIR = state_dir


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_log_level(level: str) -> str:
    value = (level or "").strip().lower()
    if value == "warnning":
        value = "warning"
    if value not in LOG_LEVELS:
        raise ValueError(f"日志等级不合法: {level}")
    return value


def append_log(
    level: str,
    category: str,
    message: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """写入一条日志记录。

    category 取值: api, command, zfs, iscsi, scheduler, system
    details 建议包含:
      - object_type: ZVOL / Snapshot / Clone / Target / Backstore / LUN / ACL / Portal / Job
      - object_name: 操作的对象名称
      - action: 操作名称 (create / delete / update / rollback / sync / clone / promote / ...)
      - result: success / failure
      - error: 错误信息（失败时）
    """
    if LOG_FILE is None or STATE_DIR is None:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "id": uuid.uuid4().hex,
        "timestamp": now_iso(),
        "level": normalize_log_level(level),
        "category": category,
        "message": message,
        "details": details or {},
    }
    line = json.dumps(record, ensure_ascii=True) + "\n"
    with log_file_lock:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line)


def read_logs(level: str = "", limit: int = 200) -> list[dict]:
    if LOG_FILE is None:
        return []
    normalized_level = normalize_log_level(level) if level else ""
    max_items = max(1, min(int(limit), 1000))
    if not LOG_FILE.exists():
        return []
    records: list[dict] = []
    with log_file_lock:
        try:
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if normalized_level and record.get("level") != normalized_level:
            continue
        records.append(record)
        if len(records) >= max_items:
            break
    return records


def summarize_log_counts(limit: int = 500) -> dict[str, int]:
    counts = {level: 0 for level in sorted(LOG_LEVELS)}
    for record in read_logs(limit=limit):
        level = record.get("level")
        if level in counts:
            counts[level] += 1
    return counts
