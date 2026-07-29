"""通用工具函数"""

import json
import platform
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from server.log_utils import append_log

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")
SAFE_DATASET_RE = re.compile(r"^[A-Za-z0-9_.:+/-]+$")
SAFE_SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9_.:+/-]+@[A-Za-z0-9_.:+-]+$")
SAFE_IQN_RE = re.compile(r"^[A-Za-z0-9.:_-]+$")
RESERVED_ISCSI_NAMES = {"discovery_auth"}

# 由 backend 初始化时设置
CONFIGFS_TARGET_ROOT: Optional[Path] = None
CONFIGFS_TARGET_CORE: Optional[Path] = None
CONFIGFS_ISCSI: Optional[Path] = None


def init(configfs_root: Path) -> None:
    global CONFIGFS_TARGET_ROOT, CONFIGFS_TARGET_CORE, CONFIGFS_ISCSI
    CONFIGFS_TARGET_ROOT = configfs_root
    CONFIGFS_TARGET_CORE = configfs_root / "core"
    CONFIGFS_ISCSI = configfs_root / "iscsi"


def ensure_supported_runtime() -> None:
    if platform.system().lower() != "linux":
        raise HTTPException(status_code=503, detail="当前不是 Linux/fnOS 环境，ZFS 与 LIO 功能不可用")
    if CONFIGFS_TARGET_ROOT and not CONFIGFS_TARGET_ROOT.exists():
        raise HTTPException(status_code=503, detail="未检测到 /sys/kernel/config/target，LIO 功能不可用")


def run_cmd(cmd: list[str], timeout: int = 30) -> str:
    append_log("debug", "command", "执行命令", {"command": cmd, "timeout": timeout})
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        append_log("error", "command", "命令不存在", {"command": cmd})
        raise HTTPException(status_code=500, detail=f"命令不存在：{cmd[0]}")
    except subprocess.TimeoutExpired:
        append_log("error", "command", "命令执行超时", {"command": cmd, "timeout": timeout})
        raise HTTPException(status_code=500, detail=f"命令超时：{' '.join(cmd)}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "命令执行失败"
        append_log("error", "command", "命令执行失败", {"command": cmd, "detail": detail, "returncode": result.returncode})
        raise HTTPException(status_code=500, detail=detail)
    append_log("debug", "command", "命令执行成功", {"command": cmd})
    return result.stdout.strip()


def run_result(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"命令不存在：{cmd[0]}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "命令超时")


def require_safe_name(value: str, label: str) -> str:
    value = value.strip()
    if not value or not SAFE_NAME_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"{label} 包含非法字符")
    return value


def require_safe_dataset(value: str, label: str) -> str:
    value = value.strip().strip("/")
    if not value or not SAFE_DATASET_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"{label} 包含非法字符")
    if ".." in value.split("/"):
        raise HTTPException(status_code=400, detail=f"{label} 不能包含上级路径")
    return value


def require_iqn(value: str, label: str = "IQN") -> str:
    value = value.strip()
    if not value.startswith("iqn.") or not SAFE_IQN_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"{label} 格式不合法")
    return value


def normalize_zvol_name(value: str) -> str:
    value = require_safe_dataset(value, "ZVOL 名称")
    if "/" not in value:
        raise HTTPException(status_code=400, detail="ZVOL 名称必须包含完整数据集路径，例如 tank/iscsi/steam")
    return value


def normalize_snapshot_name(value: str, label: str = "快照名称") -> str:
    value = value.strip()
    if not value or not SAFE_SNAPSHOT_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"{label} 格式不合法")
    dataset, snapshot = value.split("@", 1)
    normalize_zvol_name(dataset)
    require_safe_name(snapshot, "快照短名称")
    return value


def normalize_schedule_prefix(value: str) -> str:
    return require_safe_name(value, "定时快照前缀")


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_state_dir(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    temp_path.replace(path)


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def zvol_device_path(zvol_name: str) -> str:
    return f"/dev/zvol/{zvol_name}"


def default_backstore_name(zvol_name: str) -> str:
    return zvol_name.replace("/", "_")


def read_text_if_exists(path: Path) -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return ""


def parse_portal_name(value: str) -> dict:
    if ":" not in value:
        return {"value": value, "ip": value, "port": ""}
    ip, port = value.rsplit(":", 1)
    return {"value": value, "ip": ip, "port": port}


def targetcli_saveconfig() -> None:
    run_result(["targetcli", "saveconfig"], timeout=120)


def targetcli_tpg_path(iqn: str, tpg: int = 1) -> str:
    return f"/iscsi/{iqn}/tpg{tpg}"
