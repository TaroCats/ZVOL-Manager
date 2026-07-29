#!/usr/bin/env python3

import json
import os
import platform
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


APP_VERSION = "1.3.0"
ROOT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT_DIR / "frontend"
STATE_DIR = ROOT_DIR / "runtime"
SNAPSHOT_JOBS_FILE = STATE_DIR / "snapshot_jobs.json"
LOG_FILE = STATE_DIR / "operations.log"
CONFIGFS_TARGET_ROOT = Path("/sys/kernel/config/target")
CONFIGFS_TARGET_CORE = CONFIGFS_TARGET_ROOT / "core"
CONFIGFS_ISCSI = CONFIGFS_TARGET_ROOT / "iscsi"
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")
SAFE_DATASET_RE = re.compile(r"^[A-Za-z0-9_.:+/-]+$")
SAFE_SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9_.:+/-]+@[A-Za-z0-9_.:+-]+$")
SAFE_IQN_RE = re.compile(r"^[A-Za-z0-9.:_-]+$")
RESERVED_ISCSI_NAMES = {"discovery_auth"}
SCHEDULE_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
SCHEDULE_LOOP_INTERVAL = 15
snapshot_jobs_lock = threading.Lock()
log_file_lock = threading.Lock()
snapshot_jobs_started = False
LOG_LEVELS = {"debug", "info", "warning", "error"}

ARTICLE_PROFILE = {
    "pool_recommended": {
        "ashift": "12",
        "compression": "lz4",
        "atime": "off",
        "xattr": "sa",
        "acltype": "posix",
    },
    "zvol_recommended": {
        "parent_dataset": "iscsi",
        "volblocksize": "16K",
        "compression": "lz4",
        "sync": "standard",
        "sparse": False,
    },
    "lio_recommended": {
        "backend": "IBLOCK",
        "block_size": 512,
        "queue_depth": 128,
        "is_nonrot": 1,
        "emulate_write_cache": 0,
        "mcs": "按环境自行配置",
    },
    "warnings": [
        "不要把消费级 NVMe 轻易当作 SLOG 使用。",
        "文章方案的重点是 ZVOL + IBLOCK，避免 FILEIO 路径过长导致同步写过慢。",
        "厚置备 ZVOL 更符合文章中的稳定性取向。",
        "涉及 destroy、rmdir、target 删除前都应先确认没有业务数据依赖。",
        "ACL、CHAP、Portal、MCS 需要结合你的网络环境单独配置。",
    ],
}

app = FastAPI(title="ZVOL Manager", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


class CreateZvolRequest(BaseModel):
    pool: str
    name: str
    parent_dataset: str = Field(default="iscsi")
    size: str = Field(description="例如 10T、500G、64G")
    volblocksize: str = "16K"
    compression: str = "lz4"
    sync: str = "standard"
    sparse: bool = False


class CreateBackstoreRequest(BaseModel):
    zvol_name: str
    backstore_name: Optional[str] = None


class CreateSnapshotRequest(BaseModel):
    snapshot_name: str = Field(description="例如 before-upgrade")


class ReverseSyncSnapshotRequest(BaseModel):
    base_snapshot: Optional[str] = Field(default=None, description="可选，自定义增量基线快照")


class SyncOriginSnapshotRequest(BaseModel):
    clone_names: list[str] = Field(default_factory=list, description="可选，仅同步到指定 clone")


class CloneZvolRequest(BaseModel):
    snapshot_name: str = Field(description="完整快照名，例如 tank/iscsi/games@before-upgrade")
    pool: str
    parent_dataset: str = Field(default="iscsi")
    name: str


class CreateIscsiTargetRequest(BaseModel):
    iqn: str = Field(description="例如 iqn.2026-07.local.fnos:steam")
    tpg: int = 1


class CreateIscsiLunRequest(BaseModel):
    backstore_name: str
    tpg: int = 1


class PortalRequest(BaseModel):
    ip: str
    port: int = 3260
    tpg: int = 1


class AclRequest(BaseModel):
    initiator_iqn: str
    tpg: int = 1


class TargetSettingsRequest(BaseModel):
    authentication: Optional[bool] = None
    generate_node_acls: Optional[bool] = None
    userid: Optional[str] = None
    password: Optional[str] = None
    mutual_userid: Optional[str] = None
    mutual_password: Optional[str] = None
    tpg: int = 1


class AclChapRequest(BaseModel):
    userid: str
    password: str
    mutual_userid: Optional[str] = None
    mutual_password: Optional[str] = None
    tpg: int = 1


class SnapshotScheduleRequest(BaseModel):
    zvol_name: str
    prefix: str = Field(default="auto")
    interval_minutes: int = Field(description="执行周期，单位分钟")
    keep_count: int = Field(description="最多保留的快照数量")
    enabled: bool = True


def ensure_supported_runtime() -> None:
    if platform.system().lower() != "linux":
        raise HTTPException(status_code=503, detail="当前不是 Linux/fnOS 环境，ZFS 与 LIO 功能不可用")
    if not CONFIGFS_TARGET_ROOT.exists():
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
    value = require_safe_name(value, "定时快照前缀")
    return value


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: Any) -> None:
    ensure_state_dir()
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


def normalize_log_level(level: str) -> str:
    value = (level or "").strip().lower()
    if value == "warnning":
        value = "warning"
    if value not in LOG_LEVELS:
        raise HTTPException(status_code=400, detail="日志等级不合法")
    return value


def append_log(level: str, category: str, message: str, details: Optional[dict[str, Any]] = None) -> None:
    ensure_state_dir()
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
    ensure_state_dir()
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


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        if not request.url.path.startswith("/assets"):
            append_log(
                "error",
                "api",
                f"{request.method} {request.url.path}",
                {
                    "status_code": 500,
                    "duration_ms": duration_ms,
                    "query": str(request.url.query),
                    "error": str(exc),
                },
            )
        raise

    duration_ms = int((time.perf_counter() - start) * 1000)
    if request.url.path not in {"/api/logs"} and not request.url.path.startswith("/assets"):
        status_code = response.status_code
        if status_code >= 500:
            level = "error"
        elif status_code >= 400:
            level = "warning"
        elif request.method.upper() == "GET":
            level = "debug"
        else:
            level = "info"
        append_log(
            level,
            "api",
            f"{request.method} {request.url.path}",
            {
                "status_code": status_code,
                "duration_ms": duration_ms,
                "query": str(request.url.query),
            },
        )
    return response


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


def ensure_parent_dataset(full_parent: str) -> None:
    probe = run_result(["zfs", "list", "-H", "-o", "name", full_parent])
    if probe.returncode == 0:
        return
    run_cmd(["zfs", "create", "-o", "mountpoint=none", full_parent])


def get_zfs_property(dataset: str, prop: str, default: str = "-") -> str:
    result = run_result(["zfs", "get", "-H", "-o", "value", prop, dataset], timeout=30)
    if result.returncode != 0:
        return default
    value = (result.stdout or "").strip()
    return value or default


def list_zvol_rows() -> list[dict]:
    output = run_cmd(
        [
            "zfs",
            "list",
            "-H",
            "-t",
            "volume",
            "-o",
            "name,volsize,used,refer,origin",
        ]
    )
    rows: list[dict] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        rows.append(
            {
                "name": parts[0],
                "volsize": parts[1],
                "used": parts[2],
                "refer": parts[3],
                "origin": parts[4] or "-",
            }
        )
    return rows


def list_zvol_snapshots(zvol_name: str, clone_names_by_origin: Optional[dict[str, list[str]]] = None) -> list[dict]:
    result = run_result(
        [
            "zfs",
            "list",
            "-H",
            "-t",
            "snapshot",
            "-o",
            "name,used,refer",
            "-r",
            zvol_name,
        ],
        timeout=60,
    )
    if result.returncode != 0:
        return []

    snapshots: list[dict] = []
    prefix = f"{zvol_name}@"
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0].startswith(prefix):
            continue
        snapshots.append(
            {
                "name": parts[0],
                "short_name": parts[0].split("@", 1)[1],
                "used": parts[1],
                "refer": parts[2],
                "dependent_clones": clone_names_by_origin.get(parts[0], []) if clone_names_by_origin else [],
            }
        )
    return snapshots


def find_dependent_clones(snapshot_name: str) -> list[str]:
    return [row["name"] for row in list_zvol_rows() if row["origin"] == snapshot_name]


def snapshot_dataset_name(snapshot_name: str) -> str:
    return snapshot_name.split("@", 1)[0]


def build_origin_chain(zvol_name: str, rows_by_name: dict[str, dict]) -> list[str]:
    chain: list[str] = []
    seen: set[str] = set()
    current = rows_by_name.get(zvol_name)
    while current and current["origin"] and current["origin"] != "-":
        origin_snapshot = current["origin"]
        if origin_snapshot in seen:
            break
        chain.append(origin_snapshot)
        seen.add(origin_snapshot)
        current = rows_by_name.get(snapshot_dataset_name(origin_snapshot))
    return chain


def get_zvol_row_by_name(zvol_name: str) -> dict:
    for row in list_zvol_rows():
        if row["name"] == zvol_name:
            return row
    raise HTTPException(status_code=404, detail="ZVOL 不存在")


def list_dataset_snapshots(zvol_name: str) -> list[dict]:
    result = run_result(
        ["zfs", "list", "-H", "-t", "snapshot", "-o", "name,creation", "-s", "creation", "-r", zvol_name],
        timeout=60,
    )
    if result.returncode != 0:
        return []
    snapshots: list[dict] = []
    prefix = f"{zvol_name}@"
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or not parts[0].startswith(prefix):
            continue
        snapshots.append({"name": parts[0], "creation": parts[1]})
    return snapshots


def create_snapshot_impl(zvol_name: str, snapshot_short_name: str) -> dict:
    full_name = normalize_zvol_name(zvol_name)
    short_name = require_safe_name(snapshot_short_name, "快照名称")
    full_snapshot_name = f"{full_name}@{short_name}"
    run_cmd(["zfs", "snapshot", full_snapshot_name], timeout=120)
    return {
        "message": "ZVOL 快照创建成功",
        "zvol_name": full_name,
        "snapshot_name": full_snapshot_name,
    }


def destroy_snapshot_impl(snapshot_name: str, promote_dependent_clones: bool = True) -> dict:
    full_snapshot_name = normalize_snapshot_name(snapshot_name, "快照名称")
    dependent_clones = find_dependent_clones(full_snapshot_name)
    promoted_clones: list[str] = []
    if dependent_clones and not promote_dependent_clones:
        return {
            "snapshot_name": full_snapshot_name,
            "promoted_clones": [],
            "skipped_clones": dependent_clones,
        }
    for clone_name in dependent_clones:
        run_cmd(["zfs", "promote", clone_name], timeout=120)
        promoted_clones.append(clone_name)
    run_cmd(["zfs", "destroy", full_snapshot_name], timeout=120)
    return {
        "snapshot_name": full_snapshot_name,
        "promoted_clones": promoted_clones,
        "skipped_clones": [],
    }


def pipe_zfs_send_receive(base_snapshot: str, source_snapshot: str, target_dataset: str) -> None:
    preflight = run_result(["zfs", "send", "-nP", "-i", base_snapshot, source_snapshot], timeout=30)
    if preflight.returncode != 0:
        detail = (preflight.stderr or preflight.stdout or "").strip() or "增量同步预检查失败"
        raise HTTPException(status_code=500, detail=detail)

    # 目标 dataset 有快照时 zfs receive -F 仍可能失败，先清理目标上的快照
    target_snapshots = list_dataset_snapshots(target_dataset)
    if target_snapshots:
        for snap in target_snapshots:
            run_result(["zfs", "destroy", "-d", snap["name"]], timeout=30)
        # 二次确认
        remaining = list_dataset_snapshots(target_dataset)
        if remaining:
            names = ", ".join(item["name"] for item in remaining)
            raise HTTPException(
                status_code=500,
                detail=f"目标 dataset {target_dataset} 仍有 {len(remaining)} 个快照无法清理：{names}",
            )

    recv_proc = subprocess.Popen(
        ["zfs", "receive", "-F", target_dataset],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=False,
    )
    send_proc = subprocess.Popen(
        ["zfs", "send", "-i", base_snapshot, source_snapshot],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    assert send_proc.stdout is not None
    assert recv_proc.stdin is not None
    try:
        while True:
            chunk = send_proc.stdout.read(1024 * 1024)
            if not chunk:
                break
            recv_proc.stdin.write(chunk)
        recv_proc.stdin.close()
        send_stderr = send_proc.stderr.read() if send_proc.stderr is not None else b""
        send_code = send_proc.wait()
        recv_stderr = recv_proc.stderr.read() if recv_proc.stderr is not None else b""
        recv_code = recv_proc.wait()
    finally:
        if send_proc.stdout is not None:
            send_proc.stdout.close()

    if send_code != 0:
        detail = send_stderr.decode("utf-8", errors="ignore").strip() or "zfs send 执行失败"
        raise HTTPException(status_code=500, detail=detail)
    if recv_code != 0:
        detail = recv_stderr.decode("utf-8", errors="ignore").strip() or "zfs receive 执行失败"
        raise HTTPException(status_code=500, detail=detail)


def build_clone_sync_target(source_dataset: str, clone_row: dict, source_snapshot: str) -> dict:
    target_dataset = clone_row["name"]
    base_snapshot = clone_row["origin"]
    if snapshot_dataset_name(base_snapshot) != source_dataset:
        raise HTTPException(status_code=409, detail=f"Clone {target_dataset} 的 origin 不属于当前源 ZVOL")
    pipe_zfs_send_receive(base_snapshot, source_snapshot, target_dataset)
    return {
        "clone_name": target_dataset,
        "base_snapshot": base_snapshot,
        "source_snapshot": source_snapshot,
        "target_dataset": target_dataset,
        "status": "success",
    }


def read_snapshot_jobs() -> list[dict]:
    with snapshot_jobs_lock:
        jobs = read_json_file(SNAPSHOT_JOBS_FILE, [])
        return jobs if isinstance(jobs, list) else []


def write_snapshot_jobs(jobs: list[dict]) -> None:
    with snapshot_jobs_lock:
        atomic_write_json(SNAPSHOT_JOBS_FILE, jobs)


def get_snapshot_job(job_id: str) -> dict:
    for job in read_snapshot_jobs():
        if job.get("id") == job_id:
            return job
    raise HTTPException(status_code=404, detail="定时快照任务不存在")


def sanitize_snapshot_job(job: dict) -> dict:
    interval_minutes = int(job.get("interval_minutes", 0))
    keep_count = int(job.get("keep_count", 0))
    if interval_minutes < 1:
        raise HTTPException(status_code=400, detail="定时快照周期必须大于等于 1 分钟")
    if keep_count < 1:
        raise HTTPException(status_code=400, detail="保留数量必须大于等于 1")
    return {
        "id": str(job.get("id") or uuid.uuid4().hex),
        "zvol_name": normalize_zvol_name(job.get("zvol_name", "")),
        "prefix": normalize_schedule_prefix(job.get("prefix", "auto")),
        "interval_minutes": interval_minutes,
        "keep_count": keep_count,
        "enabled": bool(job.get("enabled", True)),
        "created_at": str(job.get("created_at") or now_iso()),
        "updated_at": now_iso(),
        "last_run_at": str(job.get("last_run_at") or ""),
        "last_snapshot_name": str(job.get("last_snapshot_name") or ""),
        "last_error": str(job.get("last_error") or ""),
        "last_pruned_snapshots": list(job.get("last_pruned_snapshots") or []),
        "last_skipped_snapshots": list(job.get("last_skipped_snapshots") or []),
    }


def ensure_unique_snapshot_job(job: dict, existing_jobs: list[dict], exclude_job_id: str = "") -> None:
    for current in existing_jobs:
        if exclude_job_id and current.get("id") == exclude_job_id:
            continue
        if current.get("zvol_name") == job["zvol_name"] and current.get("prefix") == job["prefix"]:
            raise HTTPException(status_code=409, detail="同一 ZVOL 已存在相同前缀的定时快照计划")


def build_snapshot_job_view(job: dict) -> dict:
    view = dict(job)
    reference_time = parse_iso_datetime(job["last_run_at"]) if job.get("last_run_at") else parse_iso_datetime(job["created_at"])
    next_run_at = reference_time + timedelta(minutes=int(job["interval_minutes"]))
    view["next_run_at"] = next_run_at.isoformat(timespec="seconds")
    return view


def list_snapshot_jobs_view() -> list[dict]:
    return [build_snapshot_job_view(job) for job in read_snapshot_jobs()]


def prune_scheduled_snapshots(zvol_name: str, prefix: str, keep_count: int) -> dict:
    snapshots = [item["name"] for item in list_dataset_snapshots(zvol_name) if item["name"].startswith(f"{zvol_name}@{prefix}-")]
    if len(snapshots) <= keep_count:
        return {"pruned": [], "skipped": []}

    pruned: list[str] = []
    skipped: list[str] = []
    for snapshot_name in snapshots[: len(snapshots) - keep_count]:
        result = destroy_snapshot_impl(snapshot_name, promote_dependent_clones=False)
        if result["skipped_clones"]:
            skipped.append(snapshot_name)
            continue
        pruned.append(snapshot_name)
    return {"pruned": pruned, "skipped": skipped}


def run_snapshot_job(job_id: str) -> dict:
    job = get_snapshot_job(job_id)
    suffix = datetime.now().strftime(SCHEDULE_TIMESTAMP_FORMAT)
    snapshot_short_name = f"{job['prefix']}-{suffix}"
    append_log("info", "scheduler", "执行定时快照任务", {"job_id": job_id, "zvol_name": job["zvol_name"], "prefix": job["prefix"]})
    snapshot_result = create_snapshot_impl(job["zvol_name"], snapshot_short_name)
    prune_result = prune_scheduled_snapshots(job["zvol_name"], job["prefix"], int(job["keep_count"]))

    jobs = read_snapshot_jobs()
    updated_jobs: list[dict] = []
    for current in jobs:
        if current.get("id") != job_id:
            updated_jobs.append(current)
            continue
        current = dict(current)
        current["last_run_at"] = now_iso()
        current["updated_at"] = now_iso()
        current["last_snapshot_name"] = snapshot_result["snapshot_name"]
        current["last_error"] = ""
        current["last_pruned_snapshots"] = prune_result["pruned"]
        current["last_skipped_snapshots"] = prune_result["skipped"]
        updated_jobs.append(current)
        job = current
    write_snapshot_jobs(updated_jobs)
    append_log(
        "info" if not prune_result["skipped"] else "warning",
        "scheduler",
        "定时快照任务完成",
        {
            "job_id": job_id,
            "snapshot_name": snapshot_result["snapshot_name"],
            "pruned": prune_result["pruned"],
            "skipped": prune_result["skipped"],
        },
    )
    return build_snapshot_job_view(job)


def snapshot_jobs_loop() -> None:
    while True:
        try:
            if platform.system().lower() == "linux":
                jobs = read_snapshot_jobs()
                now = datetime.now()
                for job in jobs:
                    if not job.get("enabled"):
                        continue
                    reference_time = parse_iso_datetime(job["last_run_at"]) if job.get("last_run_at") else parse_iso_datetime(job["created_at"])
                    due_at = reference_time + timedelta(minutes=int(job["interval_minutes"]))
                    if now < due_at:
                        continue
                    try:
                        run_snapshot_job(job["id"])
                    except Exception as exc:
                        append_log("error", "scheduler", "定时快照任务失败", {"job_id": job["id"], "error": str(exc)})
                        updated_jobs = read_snapshot_jobs()
                        for current in updated_jobs:
                            if current.get("id") == job["id"]:
                                current["last_error"] = str(exc)
                                current["updated_at"] = now_iso()
                                current["last_run_at"] = now_iso()
                        write_snapshot_jobs(updated_jobs)
        except Exception:
            pass
        time.sleep(SCHEDULE_LOOP_INTERVAL)


def read_backstores() -> list[dict]:
    backstores: list[dict] = []
    if not CONFIGFS_TARGET_CORE.exists():
        return backstores

    for iblock_dir in sorted(CONFIGFS_TARGET_CORE.glob("iblock_*")):
        if not iblock_dir.is_dir():
            continue
        for entry in sorted(iblock_dir.iterdir()):
            if not entry.is_dir():
                continue
            device = read_text_if_exists(entry / "udev_path")
            enabled = read_text_if_exists(entry / "enable") == "1"
            serial = read_text_if_exists(entry / "wwn" / "vpd_unit_serial")
            zvol_name = device.replace("/dev/zvol/", "", 1) if device.startswith("/dev/zvol/") else ""
            backstores.append(
                {
                    "name": entry.name,
                    "device": device,
                    "enabled": enabled,
                    "serial": serial,
                    "zvol_name": zvol_name,
                    "iblock_path": str(entry),
                    "iblock_group": iblock_dir.name,
                }
            )
    return backstores


def get_backstore(name: str) -> Optional[dict]:
    for item in read_backstores():
        if item["name"] == name:
            return item
    return None


def resolve_iqn_dir(iqn: str) -> Path:
    iqn_dir = CONFIGFS_ISCSI / iqn
    if not iqn_dir.exists():
        raise HTTPException(status_code=404, detail="iSCSI Target 不存在")
    return iqn_dir


def resolve_tpg_dir(iqn: str, tpg: int = 1) -> Path:
    iqn_dir = resolve_iqn_dir(iqn)
    candidates = [iqn_dir / f"tpgt_{tpg}", iqn_dir / f"tpg{tpg}"]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    all_tpgs = iter_tpg_dirs(iqn_dir)
    if len(all_tpgs) == 1:
        return all_tpgs[0]
    raise HTTPException(status_code=404, detail="未找到对应的 TPG")


def read_auth_config(auth_dir: Path) -> dict:
    userid = read_text_if_exists(auth_dir / "userid")
    password = read_text_if_exists(auth_dir / "password")
    mutual_userid = read_text_if_exists(auth_dir / "mutual_userid")
    mutual_password = read_text_if_exists(auth_dir / "mutual_password")
    return {
        "userid": userid,
        "password_set": bool(password),
        "mutual_userid": mutual_userid,
        "mutual_password_set": bool(mutual_password),
    }


def read_tpg_settings(iqn: str, tpg_dir: Path) -> dict:
    attrib_dir = tpg_dir / "attrib"
    auth_dir = tpg_dir / "auth"
    auth_config = read_auth_config(auth_dir)
    return {
        "iqn": iqn,
        "tpg": tpg_dir.name,
        "authentication": read_text_if_exists(attrib_dir / "authentication") == "1",
        "generate_node_acls": read_text_if_exists(attrib_dir / "generate_node_acls") == "1",
        "demo_mode_write_protect": read_text_if_exists(attrib_dir / "demo_mode_write_protect") == "1",
        **auth_config,
        "mcs_hint": "LIO 没有单独的 MCS 开关；通常通过多 portal/多路径和 initiator 侧多连接或 multipath 来实现。",
    }


def read_acl_entry(acl_item: Path) -> dict:
    auth = read_auth_config(acl_item / "auth")
    return {
        "initiator_iqn": acl_item.name,
        **auth,
    }


def iter_tpg_dirs(iqn_dir: Path) -> list[Path]:
    tpgs = []
    for child in iqn_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("tpgt_") or child.name.startswith("tpg"):
            tpgs.append(child)
    return sorted(tpgs, key=lambda p: p.name)


def read_lun_backstores(lun_dir: Path) -> list[str]:
    backstore_names: set[str] = set()
    for entry in sorted(lun_dir.rglob("*")):
        if not entry.is_symlink():
            continue
        try:
            real_path = Path(os.path.realpath(entry))
        except OSError:
            continue
        if CONFIGFS_TARGET_CORE not in real_path.parents:
            continue
        if real_path.name:
            backstore_names.add(real_path.name)
    return sorted(backstore_names)


def iter_lun_dirs(tpg_dir: Path) -> list[Path]:
    lun_roots = [tpg_dir / "lun", tpg_dir / "luns"]
    lun_dirs: list[Path] = []
    seen: set[Path] = set()
    for lun_root in lun_roots:
        if not lun_root.exists():
            continue
        for lun_dir in sorted(lun_root.iterdir()):
            if not lun_dir.is_dir() or lun_dir in seen:
                continue
            seen.add(lun_dir)
            lun_dirs.append(lun_dir)
    return lun_dirs


def read_iscsi_targets() -> list[dict]:
    targets: list[dict] = []
    if not CONFIGFS_ISCSI.exists():
        return targets

    backstores_by_name = {item["name"]: item for item in read_backstores()}
    for iqn_dir in sorted(CONFIGFS_ISCSI.iterdir()):
        if not iqn_dir.is_dir():
            continue
        if iqn_dir.name in RESERVED_ISCSI_NAMES:
            continue

        tpg_items = []
        used_backstores: set[str] = set()
        target_portals: set[str] = set()
        target_acls: list[dict] = []

        for tpg_dir in iter_tpg_dirs(iqn_dir):
            lun_items = []
            portals: list[dict] = []
            acl_items: list[dict] = []
            for lun_dir in iter_lun_dirs(tpg_dir):
                linked_backstores = read_lun_backstores(lun_dir)
                used_backstores.update(linked_backstores)
                lun_items.append({"name": lun_dir.name, "backstores": linked_backstores})

            np_dir = tpg_dir / "np"
            if np_dir.exists():
                for portal_dir in sorted(np_dir.iterdir()):
                    if portal_dir.is_dir():
                        portal = parse_portal_name(portal_dir.name)
                        portals.append(portal)
                        target_portals.add(portal["value"])

            acl_dir = tpg_dir / "acls"
            if acl_dir.exists():
                for acl_item in sorted(acl_dir.iterdir()):
                    if acl_item.is_dir():
                        acl_info = read_acl_entry(acl_item)
                        acl_items.append(acl_info)
                        target_acls.append(acl_info)

            tpg_items.append(
                {
                    "name": tpg_dir.name,
                    "luns": lun_items,
                    "portals": portals,
                    "acls": acl_items,
                    "settings": read_tpg_settings(iqn_dir.name, tpg_dir),
                }
            )

        zvol_names = []
        for backstore_name in sorted(used_backstores):
            backstore = backstores_by_name.get(backstore_name)
            if backstore and backstore["zvol_name"]:
                zvol_names.append(backstore["zvol_name"])

        targets.append(
            {
                "iqn": iqn_dir.name,
                "tpgs": tpg_items,
                "backstores": sorted(used_backstores),
                "zvol_names": zvol_names,
                "portals": sorted(target_portals),
                "acl_names": [item["initiator_iqn"] for item in target_acls],
                "acls": target_acls,
                "settings": tpg_items[0]["settings"] if tpg_items else None,
            }
        )
    return targets


def get_target(iqn: str) -> Optional[dict]:
    for item in read_iscsi_targets():
        if item["iqn"] == iqn:
            return item
    return None


def backstore_is_used(backstore_name: str) -> bool:
    for target in read_iscsi_targets():
        if backstore_name in target["backstores"]:
            return True
    return False


def create_backstore_impl(zvol_name: str, backstore_name: str) -> dict:
    if get_backstore(backstore_name):
        raise HTTPException(status_code=409, detail="Backstore 已存在")

    device = zvol_device_path(zvol_name)
    if not Path(device).exists():
        raise HTTPException(status_code=404, detail=f"ZVOL 设备不存在：{device}")

    run_cmd(
        [
            "targetcli",
            "/backstores/block",
            "create",
            f"name={backstore_name}",
            f"dev={device}",
        ],
        timeout=120,
    )

    created = get_backstore(backstore_name)
    if not created:
        raise HTTPException(status_code=500, detail="Backstore 创建命令已执行，但未在 configfs 中找到结果")
    targetcli_saveconfig()
    return created


@app.get("/api/version")
def get_version():
    return {"version": APP_VERSION}


@app.get("/api/profile")
def get_article_profile():
    return ARTICLE_PROFILE


@app.get("/api/health")
def get_health():
    commands = {}
    for cmd in ("zfs", "zpool", "targetcli"):
        result = run_result([cmd, "--version"], timeout=10)
        commands[cmd] = {
            "available": result.returncode != 127,
            "detail": (result.stdout or result.stderr).strip()[:200],
        }

    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "linux_supported": platform.system().lower() == "linux",
        "configfs_target_exists": CONFIGFS_TARGET_ROOT.exists(),
        "commands": commands,
        "backstore_count": len(read_backstores()) if CONFIGFS_TARGET_CORE.exists() else 0,
        "target_count": len(read_iscsi_targets()) if CONFIGFS_ISCSI.exists() else 0,
        "snapshot_job_count": len(read_snapshot_jobs()),
        "log_counts": summarize_log_counts(),
    }


@app.get("/api/pools")
def list_pools():
    ensure_supported_runtime()
    output = run_cmd(["zpool", "list", "-H", "-o", "name,size,alloc,free,health"])
    pools = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        pools.append(
            {
                "name": parts[0],
                "size": parts[1],
                "alloc": parts[2],
                "free": parts[3],
                "health": parts[4],
            }
        )
    return {"pools": pools}


@app.get("/api/zvols")
def list_zvols():
    ensure_supported_runtime()
    zvol_rows = list_zvol_rows()
    rows_by_name = {row["name"]: row for row in zvol_rows}
    backstores = {item["zvol_name"]: item for item in read_backstores()}
    targets = read_iscsi_targets()
    iqn_by_backstore: dict[str, list[str]] = {}
    clone_names_by_origin: dict[str, list[str]] = {}
    for target in targets:
        for backstore_name in target["backstores"]:
            iqn_by_backstore.setdefault(backstore_name, []).append(target["iqn"])
    for row in zvol_rows:
        if row["origin"] and row["origin"] != "-":
            clone_names_by_origin.setdefault(row["origin"], []).append(row["name"])

    zvols = []
    for row in zvol_rows:
        name = row["name"]
        snapshots = list_zvol_snapshots(name, clone_names_by_origin)
        backstore = backstores.get(name)
        target_iqns = []
        if backstore:
            target_iqns = iqn_by_backstore.get(backstore["name"], [])
        zvols.append(
            {
                "name": name,
                "device": zvol_device_path(name),
                "volsize": row["volsize"],
                "used": row["used"],
                "refer": row["refer"],
                "compressratio": get_zfs_property(name, "compressratio"),
                "volblocksize": get_zfs_property(name, "volblocksize"),
                "compression": get_zfs_property(name, "compression"),
                "sync": get_zfs_property(name, "sync"),
                "refreservation": get_zfs_property(name, "refreservation"),
                "origin": row["origin"],
                "is_clone": row["origin"] != "-",
                "origin_dataset": snapshot_dataset_name(row["origin"]) if row["origin"] != "-" else "",
                "origin_chain": build_origin_chain(name, rows_by_name),
                "snapshots": snapshots,
                "snapshot_count": len(snapshots),
                "backstore": backstore,
                "iscsi_targets": target_iqns,
            }
        )
    return {"zvols": zvols}


@app.post("/api/zvols")
def create_zvol(payload: CreateZvolRequest):
    ensure_supported_runtime()
    pool = require_safe_name(payload.pool, "存储池名称")
    name = require_safe_name(payload.name, "ZVOL 名称")
    parent_dataset = require_safe_dataset(payload.parent_dataset, "父数据集")
    full_parent = f"{pool}/{parent_dataset}"
    full_name = f"{full_parent}/{name}"

    ensure_parent_dataset(full_parent)

    args = [
        "zfs",
        "create",
        "-V",
        payload.size.strip(),
        "-b",
        payload.volblocksize.strip(),
        "-o",
        f"compression={payload.compression.strip()}",
        "-o",
        f"sync={payload.sync.strip()}",
        full_name,
    ]
    if payload.sparse:
        args.insert(2, "-s")
    run_cmd(args, timeout=120)

    return {
        "message": "ZVOL 创建成功",
        "zvol_name": full_name,
        "device": zvol_device_path(full_name),
        "article_defaults": ARTICLE_PROFILE["zvol_recommended"],
    }


@app.delete("/api/zvols/{zvol_name:path}")
def delete_zvol(zvol_name: str):
    ensure_supported_runtime()
    full_name = normalize_zvol_name(zvol_name)
    bound_backstores = [item for item in read_backstores() if item["zvol_name"] == full_name]
    if bound_backstores:
        names = ", ".join(item["name"] for item in bound_backstores)
        raise HTTPException(status_code=409, detail=f"ZVOL 仍绑定 backstore：{names}，请先删除 backstore 或 iSCSI target")
    run_cmd(["zfs", "destroy", full_name], timeout=120)
    return {"message": "ZVOL 删除成功", "zvol_name": full_name}


@app.post("/api/zvols/{zvol_name:path}/snapshots")
def create_zvol_snapshot(zvol_name: str, payload: CreateSnapshotRequest):
    ensure_supported_runtime()
    return create_snapshot_impl(zvol_name, payload.snapshot_name)


@app.delete("/api/zvol-snapshots/{snapshot_name:path}")
def delete_zvol_snapshot(snapshot_name: str):
    ensure_supported_runtime()
    result = destroy_snapshot_impl(snapshot_name, promote_dependent_clones=True)
    return {
        "message": "ZVOL 快照删除成功",
        "snapshot_name": result["snapshot_name"],
        "promoted_clones": result["promoted_clones"],
    }


@app.post("/api/zvol-snapshots/{snapshot_name:path}/rollback")
def rollback_zvol_snapshot(snapshot_name: str):
    ensure_supported_runtime()
    full_snapshot_name = normalize_snapshot_name(snapshot_name, "快照名称")
    run_cmd(["zfs", "rollback", "-r", full_snapshot_name], timeout=120)
    return {
        "message": "ZVOL 快照回滚成功",
        "snapshot_name": full_snapshot_name,
        "zvol_name": snapshot_dataset_name(full_snapshot_name),
    }


@app.post("/api/zvol-snapshots/{snapshot_name:path}/reverse-sync")
def reverse_sync_zvol_snapshot(snapshot_name: str, payload: ReverseSyncSnapshotRequest):
    ensure_supported_runtime()
    full_snapshot_name = normalize_snapshot_name(snapshot_name, "快照名称")
    source_dataset = snapshot_dataset_name(full_snapshot_name)
    source_row = get_zvol_row_by_name(source_dataset)
    origin_snapshot = source_row.get("origin") or "-"
    if origin_snapshot == "-":
        raise HTTPException(status_code=409, detail="当前快照所属 ZVOL 不是 clone，无法执行增量反向同步")

    target_dataset = snapshot_dataset_name(origin_snapshot)
    get_zvol_row_by_name(target_dataset)
    base_snapshot = normalize_snapshot_name(payload.base_snapshot, "增量基线快照") if payload.base_snapshot else origin_snapshot
    if snapshot_dataset_name(base_snapshot) != target_dataset:
        raise HTTPException(status_code=400, detail="所选增量基线快照不属于 origin 数据集")
    pipe_zfs_send_receive(base_snapshot, full_snapshot_name, target_dataset)

    return {
        "message": "增量反向同步成功",
        "base_snapshot": base_snapshot,
        "source_snapshot": full_snapshot_name,
        "target_dataset": target_dataset,
    }


@app.post("/api/zvol-snapshots/{snapshot_name:path}/sync-to-clones")
def sync_origin_snapshot_to_clones(snapshot_name: str, payload: SyncOriginSnapshotRequest):
    ensure_supported_runtime()
    source_snapshot = normalize_snapshot_name(snapshot_name, "快照名称")
    source_dataset = snapshot_dataset_name(source_snapshot)
    source_row = get_zvol_row_by_name(source_dataset)
    if source_row.get("origin") and source_row.get("origin") != "-":
        raise HTTPException(status_code=409, detail="只有 origin ZVOL 的快照才支持同步到 clone")

    requested_clone_names = {normalize_zvol_name(item) for item in payload.clone_names}
    candidates = [
        row for row in list_zvol_rows()
        if row.get("origin") and row["origin"] != "-" and snapshot_dataset_name(row["origin"]) == source_dataset
    ]
    if requested_clone_names:
        candidates = [row for row in candidates if row["name"] in requested_clone_names]
    if not candidates:
        raise HTTPException(status_code=404, detail="当前快照没有可同步的 clone")

    results: list[dict] = []
    failures: list[dict] = []
    for clone_row in candidates:
        try:
            results.append(build_clone_sync_target(source_dataset, clone_row, source_snapshot))
        except HTTPException as exc:
            failures.append(
                {
                    "clone_name": clone_row["name"],
                    "status": "failed",
                    "detail": exc.detail,
                }
            )
    return {
        "message": "origin 增量同步已执行",
        "source_snapshot": source_snapshot,
        "results": results,
        "failures": failures,
    }


@app.post("/api/zvols/clones")
def clone_zvol(payload: CloneZvolRequest):
    ensure_supported_runtime()
    source_snapshot = normalize_snapshot_name(payload.snapshot_name, "源快照")
    pool = require_safe_name(payload.pool, "存储池名称")
    parent_dataset = require_safe_dataset(payload.parent_dataset, "父数据集")
    clone_name = require_safe_name(payload.name, "克隆名称")
    target_parent = f"{pool}/{parent_dataset}"
    target_name = f"{target_parent}/{clone_name}"

    ensure_parent_dataset(target_parent)
    run_cmd(["zfs", "clone", source_snapshot, target_name], timeout=120)
    return {
        "message": "ZVOL 克隆创建成功",
        "source_snapshot": source_snapshot,
        "zvol_name": target_name,
        "device": zvol_device_path(target_name),
    }


@app.get("/api/snapshot-jobs")
def list_snapshot_jobs():
    ensure_supported_runtime()
    return {"jobs": list_snapshot_jobs_view()}


@app.get("/api/logs")
def list_logs(
    level: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=1000),
):
    return {
        "logs": read_logs(level=level, limit=limit),
        "counts": summarize_log_counts(),
        "levels": ["debug", "info", "warning", "error"],
    }


@app.post("/api/snapshot-jobs")
def create_snapshot_job(payload: SnapshotScheduleRequest):
    ensure_supported_runtime()
    jobs = read_snapshot_jobs()
    job = sanitize_snapshot_job(payload.model_dump())
    ensure_unique_snapshot_job(job, jobs)
    jobs.append(job)
    write_snapshot_jobs(jobs)
    return {"message": "定时快照任务创建成功", "job": build_snapshot_job_view(job)}


@app.put("/api/snapshot-jobs/{job_id}")
def update_snapshot_job(job_id: str, payload: SnapshotScheduleRequest):
    ensure_supported_runtime()
    jobs = read_snapshot_jobs()
    updated_job: Optional[dict] = None
    for index, current in enumerate(jobs):
        if current.get("id") != job_id:
            continue
        merged = dict(current)
        merged.update(payload.model_dump())
        merged["id"] = job_id
        updated_job = sanitize_snapshot_job(merged)
        ensure_unique_snapshot_job(updated_job, jobs, exclude_job_id=job_id)
        jobs[index] = updated_job
        break
    if not updated_job:
        raise HTTPException(status_code=404, detail="定时快照任务不存在")
    write_snapshot_jobs(jobs)
    return {"message": "定时快照任务已更新", "job": build_snapshot_job_view(updated_job)}


@app.delete("/api/snapshot-jobs/{job_id}")
def delete_snapshot_job(job_id: str):
    ensure_supported_runtime()
    jobs = read_snapshot_jobs()
    remaining = [job for job in jobs if job.get("id") != job_id]
    if len(remaining) == len(jobs):
        raise HTTPException(status_code=404, detail="定时快照任务不存在")
    write_snapshot_jobs(remaining)
    return {"message": "定时快照任务已删除", "job_id": job_id}


@app.get("/api/backstores")
def list_backstores():
    ensure_supported_runtime()
    backstores = read_backstores()
    for item in backstores:
        item["in_use"] = backstore_is_used(item["name"])
    return {"backstores": backstores}


@app.post("/api/backstores")
def create_backstore(payload: CreateBackstoreRequest):
    ensure_supported_runtime()
    zvol_name = normalize_zvol_name(payload.zvol_name)
    backstore_name = require_safe_name(
        payload.backstore_name or default_backstore_name(zvol_name),
        "Backstore 名称",
    )
    created = create_backstore_impl(zvol_name, backstore_name)
    return {"message": "Backstore 创建成功", "backstore": created}


@app.delete("/api/backstores/{backstore_name}")
def delete_backstore(backstore_name: str):
    ensure_supported_runtime()
    name = require_safe_name(backstore_name, "Backstore 名称")
    if not get_backstore(name):
        raise HTTPException(status_code=404, detail="Backstore 不存在")
    if backstore_is_used(name):
        raise HTTPException(status_code=409, detail="Backstore 仍被 iSCSI target 使用，请先删除 target")

    run_cmd(["targetcli", "/backstores/block", "delete", name], timeout=120)

    if get_backstore(name):
        raise HTTPException(status_code=500, detail="Backstore 删除后重新扫描仍存在，请到 fnOS 上进一步排查")
    targetcli_saveconfig()
    return {"message": "Backstore 删除成功", "backstore_name": name}


@app.get("/api/iscsi/targets")
def list_iscsi_targets():
    ensure_supported_runtime()
    return {"targets": read_iscsi_targets()}


@app.post("/api/iscsi/targets")
def create_iscsi_target(payload: CreateIscsiTargetRequest):
    ensure_supported_runtime()
    iqn = require_iqn(payload.iqn)
    if payload.tpg < 1:
        raise HTTPException(status_code=400, detail="TPG 必须大于等于 1")

    if get_target(iqn):
        raise HTTPException(status_code=409, detail="iSCSI Target 已存在")

    try:
        run_cmd(["targetcli", "/iscsi", "create", iqn], timeout=120)
    except HTTPException:
        if get_target(iqn):
            run_result(["targetcli", "/iscsi", "delete", iqn], timeout=120)
        raise

    target = get_target(iqn)
    if not target:
        raise HTTPException(status_code=500, detail="Target 创建命令已执行，但未在 configfs 中找到结果")
    targetcli_saveconfig()
    return {
        "message": "iSCSI Target 创建成功",
        "target": target,
    }


@app.post("/api/iscsi/targets/{iqn}/luns")
def create_iscsi_lun(iqn: str, payload: CreateIscsiLunRequest):
    ensure_supported_runtime()
    iqn = require_iqn(iqn)
    backstore_name = require_safe_name(payload.backstore_name, "Backstore 名称")
    if payload.tpg < 1:
        raise HTTPException(status_code=400, detail="TPG 必须大于等于 1")
    target = get_target(iqn)
    if not target:
        raise HTTPException(status_code=404, detail="iSCSI Target 不存在")
    if not get_backstore(backstore_name):
        raise HTTPException(status_code=404, detail="Backstore 不存在")
    if backstore_name in target["backstores"]:
        raise HTTPException(status_code=409, detail="该 Backstore 已绑定到当前 Target")

    tpg_path = targetcli_tpg_path(iqn, payload.tpg)
    run_cmd(
        [
            "targetcli",
            f"{tpg_path}/luns",
            "create",
            f"/backstores/block/{backstore_name}",
        ],
        timeout=120,
    )
    targetcli_saveconfig()
    updated_target = get_target(iqn)
    if not updated_target or backstore_name not in updated_target["backstores"]:
        raise HTTPException(status_code=500, detail="LUN 创建命令已执行，但重新扫描 target 状态时未发现该 backstore")
    return {"message": "LUN 创建成功", "target": updated_target, "backstore_name": backstore_name}


@app.delete("/api/iscsi/targets/{iqn}")
def delete_iscsi_target(
    iqn: str,
    delete_backstore: bool = Query(default=False, description="删除 target 后顺带清理 backstore"),
):
    ensure_supported_runtime()
    iqn = require_iqn(iqn)
    target = get_target(iqn)
    if not target:
        raise HTTPException(status_code=404, detail="iSCSI Target 不存在")

    used_backstores = list(target["backstores"])
    run_cmd(["targetcli", "/iscsi", "delete", iqn], timeout=120)

    if get_target(iqn):
        raise HTTPException(status_code=500, detail="Target 删除后重新扫描仍存在")

    deleted_backstores = []
    if delete_backstore:
        for backstore_name in used_backstores:
            if get_backstore(backstore_name) and not backstore_is_used(backstore_name):
                run_result(["targetcli", "/backstores/block", "delete", backstore_name], timeout=120)
                if not get_backstore(backstore_name):
                    deleted_backstores.append(backstore_name)

    targetcli_saveconfig()
    return {
        "message": "iSCSI Target 删除成功",
        "iqn": iqn,
        "deleted_backstores": deleted_backstores,
    }


@app.get("/api/iscsi/targets/{iqn}/settings")
def get_iscsi_target_settings(iqn: str, tpg: int = Query(default=1)):
    ensure_supported_runtime()
    iqn = require_iqn(iqn)
    tpg_dir = resolve_tpg_dir(iqn, tpg)
    return {"settings": read_tpg_settings(iqn, tpg_dir)}


@app.put("/api/iscsi/targets/{iqn}/settings")
def update_iscsi_target_settings(iqn: str, payload: TargetSettingsRequest):
    ensure_supported_runtime()
    iqn = require_iqn(iqn)
    tpg_path = targetcli_tpg_path(iqn, payload.tpg)

    if payload.authentication is not None:
        run_cmd(
            [
                "targetcli",
                tpg_path,
                "set",
                "attribute",
                f"authentication={1 if payload.authentication else 0}",
            ],
            timeout=120,
        )

    if payload.generate_node_acls is not None:
        run_cmd(
            [
                "targetcli",
                tpg_path,
                "set",
                "attribute",
                f"generate_node_acls={1 if payload.generate_node_acls else 0}",
            ],
            timeout=120,
        )

    auth_updates = []
    if payload.userid is not None:
        auth_updates.append(f"userid={payload.userid}")
    if payload.password is not None:
        auth_updates.append(f"password={payload.password}")
    if payload.mutual_userid is not None:
        auth_updates.append(f"mutual_userid={payload.mutual_userid}")
    if payload.mutual_password is not None:
        auth_updates.append(f"mutual_password={payload.mutual_password}")

    if auth_updates:
        run_cmd(["targetcli", tpg_path, "set", "auth", *auth_updates], timeout=120)

    targetcli_saveconfig()
    tpg_dir = resolve_tpg_dir(iqn, payload.tpg)
    return {"message": "Target 设置已更新", "settings": read_tpg_settings(iqn, tpg_dir)}


@app.post("/api/iscsi/targets/{iqn}/portals")
def add_iscsi_portal(iqn: str, payload: PortalRequest):
    ensure_supported_runtime()
    iqn = require_iqn(iqn)
    tpg_path = targetcli_tpg_path(iqn, payload.tpg)
    run_cmd(
        ["targetcli", f"{tpg_path}/portals", "create", payload.ip.strip(), str(payload.port)],
        timeout=120,
    )
    targetcli_saveconfig()
    return {"message": "Portal 创建成功", "target": get_target(iqn)}


@app.delete("/api/iscsi/targets/{iqn}/portals")
def delete_iscsi_portal(
    iqn: str,
    ip: str = Query(...),
    port: int = Query(default=3260),
    tpg: int = Query(default=1),
):
    ensure_supported_runtime()
    iqn = require_iqn(iqn)
    tpg_path = targetcli_tpg_path(iqn, tpg)
    run_cmd(["targetcli", f"{tpg_path}/portals", "delete", ip.strip(), str(port)], timeout=120)
    targetcli_saveconfig()
    return {"message": "Portal 删除成功", "target": get_target(iqn)}


@app.post("/api/iscsi/targets/{iqn}/acls")
def add_iscsi_acl(iqn: str, payload: AclRequest):
    ensure_supported_runtime()
    iqn = require_iqn(iqn)
    initiator_iqn = require_iqn(payload.initiator_iqn, "Initiator IQN")
    tpg_path = targetcli_tpg_path(iqn, payload.tpg)
    run_cmd(["targetcli", f"{tpg_path}/acls", "create", initiator_iqn], timeout=120)
    targetcli_saveconfig()
    return {"message": "ACL 创建成功", "target": get_target(iqn)}


@app.delete("/api/iscsi/targets/{iqn}/acls")
def delete_iscsi_acl(
    iqn: str,
    initiator_iqn: str = Query(...),
    tpg: int = Query(default=1),
):
    ensure_supported_runtime()
    iqn = require_iqn(iqn)
    initiator_iqn = require_iqn(initiator_iqn, "Initiator IQN")
    tpg_path = targetcli_tpg_path(iqn, tpg)
    run_cmd(["targetcli", f"{tpg_path}/acls", "delete", initiator_iqn], timeout=120)
    targetcli_saveconfig()
    return {"message": "ACL 删除成功", "target": get_target(iqn)}


@app.put("/api/iscsi/targets/{iqn}/acls/{initiator_iqn}/chap")
def update_iscsi_acl_chap(iqn: str, initiator_iqn: str, payload: AclChapRequest):
    ensure_supported_runtime()
    iqn = require_iqn(iqn)
    initiator_iqn = require_iqn(initiator_iqn, "Initiator IQN")
    tpg_path = targetcli_tpg_path(iqn, payload.tpg)
    auth_updates = [f"userid={payload.userid}", f"password={payload.password}"]
    if payload.mutual_userid is not None:
        auth_updates.append(f"mutual_userid={payload.mutual_userid}")
    if payload.mutual_password is not None:
        auth_updates.append(f"mutual_password={payload.mutual_password}")
    run_cmd(
        ["targetcli", f"{tpg_path}/acls/{initiator_iqn}", "set", "auth", *auth_updates],
        timeout=120,
    )
    targetcli_saveconfig()
    return {"message": "ACL CHAP 已更新", "target": get_target(iqn)}


@app.on_event("startup")
def startup_snapshot_jobs_worker():
    global snapshot_jobs_started
    ensure_state_dir()
    if snapshot_jobs_started:
        return
    worker = threading.Thread(target=snapshot_jobs_loop, name="snapshot-jobs-worker", daemon=True)
    worker.start()
    snapshot_jobs_started = True


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
