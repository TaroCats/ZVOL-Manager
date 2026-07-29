"""定时快照任务管理"""

import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from server.log_utils import append_log
from server.utils import (
    normalize_zvol_name,
    normalize_schedule_prefix,
    now_iso,
    parse_iso_datetime,
    atomic_write_json,
    read_json_file,
)
from server.zfs_ops import (
    create_snapshot_impl,
    destroy_snapshot_impl,
    list_dataset_snapshots,
)

SCHEDULE_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
SCHEDULE_LOOP_INTERVAL = 15

snapshot_jobs_lock = threading.Lock()
snapshot_jobs_started = False

# 由 backend 初始化时设置
SNAPSHOT_JOBS_FILE: Optional[Path] = None


def init(jobs_file: Path) -> None:
    global SNAPSHOT_JOBS_FILE
    SNAPSHOT_JOBS_FILE = jobs_file


def read_snapshot_jobs() -> list[dict]:
    if SNAPSHOT_JOBS_FILE is None:
        return []
    with snapshot_jobs_lock:
        jobs = read_json_file(SNAPSHOT_JOBS_FILE, [])
        return jobs if isinstance(jobs, list) else []


def write_snapshot_jobs(jobs: list[dict]) -> None:
    if SNAPSHOT_JOBS_FILE is None:
        return
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
    append_log("info", "scheduler", "执行定时快照任务", {
        "object_type": "Job", "object_name": job_id,
        "action": "execute", "result": "running",
        "zvol_name": job["zvol_name"], "prefix": job["prefix"],
    })
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
    append_log("info" if not prune_result["skipped"] else "warning", "scheduler", "定时快照任务完成", {
        "object_type": "Job", "object_name": job_id,
        "action": "execute", "result": "success" if not prune_result["skipped"] else "partial",
        "snapshot_name": snapshot_result["snapshot_name"],
        "pruned": prune_result["pruned"],
        "skipped": prune_result["skipped"],
    })
    return build_snapshot_job_view(job)


def snapshot_jobs_loop() -> None:
    import platform
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
                        append_log("error", "scheduler", "定时快照任务失败", {
                            "object_type": "Job", "object_name": job["id"],
                            "action": "execute", "result": "failure",
                            "error": str(exc),
                        })
                        # 重新读取任务列表避免竞态条件
                        updated_jobs = read_snapshot_jobs()
                        for current in updated_jobs:
                            if current.get("id") == job["id"]:
                                current["last_error"] = str(exc)
                                current["updated_at"] = now_iso()
                                current["last_run_at"] = now_iso()
                        write_snapshot_jobs(updated_jobs)
        except Exception as exc:
            append_log("error", "scheduler", "定时快照循环异常", {
                "action": "loop", "result": "failure", "error": str(exc),
            })
        time.sleep(SCHEDULE_LOOP_INTERVAL)


def start_worker() -> None:
    global snapshot_jobs_started
    if snapshot_jobs_started:
        return
    worker = threading.Thread(target=snapshot_jobs_loop, name="snapshot-jobs-worker", daemon=True)
    worker.start()
    snapshot_jobs_started = True
