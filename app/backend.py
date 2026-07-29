#!/usr/bin/env python3
"""ZVOL Manager - FastAPI 主入口"""

import json
import platform
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server import log_utils
from server import utils
from server import scheduler
from server.models import (
    AclChapRequest,
    AclRequest,
    CloneZvolRequest,
    CreateBackstoreRequest,
    CreateIscsiLunRequest,
    CreateIscsiTargetRequest,
    CreateSnapshotRequest,
    CreateZvolRequest,
    PortalRequest,
    ReverseSyncSnapshotRequest,
    SnapshotScheduleRequest,
    SyncOriginSnapshotRequest,
    TargetSettingsRequest,
)
from server.zfs_ops import (
    build_origin_chain,
    clone_zvol_impl,
    create_snapshot_impl,
    create_zvol_impl,
    delete_zvol_impl,
    destroy_snapshot_impl,
    get_zfs_property,
    get_zvol_row_by_name,
    list_zvol_rows,
    list_zvol_snapshots,
    reverse_sync_impl,
    rollback_snapshot_impl,
    snapshot_dataset_name,
    sync_origin_to_clones_impl,
    zvol_device_path,
)
from server.iscsi_ops import (
    add_acl_impl,
    add_portal_impl,
    backstore_is_used,
    create_backstore_impl,
    create_iscsi_lun_impl,
    create_iscsi_target_impl,
    delete_acl_impl,
    delete_backstore_impl,
    delete_iscsi_target_impl,
    delete_portal_impl,
    read_backstores,
    read_iscsi_targets,
    read_tpg_settings,
    resolve_tpg_dir,
    update_acl_chap_impl,
    update_target_settings_impl,
)
from server.scheduler import (
    build_snapshot_job_view,
    list_snapshot_jobs_view,
    read_snapshot_jobs,
    sanitize_snapshot_job,
    ensure_unique_snapshot_job,
    start_worker as start_scheduler_worker,
    write_snapshot_jobs,
)

# ---- 路径常量 ----
ROOT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT_DIR / "frontend"
STATE_DIR = ROOT_DIR / "runtime"
LOG_FILE = STATE_DIR / "operations.log"
SNAPSHOT_JOBS_FILE = STATE_DIR / "snapshot_jobs.json"
CONFIGFS_TARGET_ROOT = Path("/sys/kernel/config/target")
CONFIGFS_TARGET_CORE = CONFIGFS_TARGET_ROOT / "core"
CONFIGFS_ISCSI = CONFIGFS_TARGET_ROOT / "iscsi"

APP_VERSION = "1.4.0"

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

# ---- 初始化子模块 ----
log_utils.init(LOG_FILE, STATE_DIR)
utils.init(CONFIGFS_TARGET_ROOT)
scheduler.init(SNAPSHOT_JOBS_FILE)

# ---- FastAPI App ----
app = FastAPI(title="ZVOL Manager", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


# ---- 中间件 ----
@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        if not request.url.path.startswith("/assets"):
            detail = getattr(exc, "detail", str(exc))
            log_utils.append_log("error", "api", f"{request.method} {request.url.path}", {
                "status_code": getattr(exc, "status_code", 500),
                "duration_ms": duration_ms,
                "query": str(request.url.query),
                "error": str(exc),
                "detail": detail,
            })
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

        body_detail = ""
        if hasattr(response, "body") and response.body:
            try:
                body_data = json.loads(response.body.decode("utf-8"))
                if isinstance(body_data, dict):
                    body_detail = body_data.get("message") or body_data.get("detail") or ""
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, AttributeError):
                pass

        log_utils.append_log(level, "api", f"{request.method} {request.url.path}", {
            "status_code": status_code,
            "duration_ms": duration_ms,
            "query": str(request.url.query),
            "detail": body_detail,
        })
    return response


# ---- 辅助函数 ----
def ensure_supported_runtime() -> None:
    utils.ensure_supported_runtime()


# ---- API 路由 ----

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
        result = utils.run_result([cmd, "--version"], timeout=10)
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
        "log_counts": log_utils.summarize_log_counts(),
    }


@app.get("/api/pools")
def list_pools():
    ensure_supported_runtime()
    output = utils.run_cmd(["zpool", "list", "-H", "-o", "name,size,alloc,free,health"])
    pools = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        pools.append({"name": parts[0], "size": parts[1], "alloc": parts[2], "free": parts[3], "health": parts[4]})
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
        zvols.append({
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
        })
    return {"zvols": zvols}


@app.post("/api/zvols")
def create_zvol(payload: CreateZvolRequest):
    ensure_supported_runtime()
    pool = utils.require_safe_name(payload.pool, "存储池名称")
    name = utils.require_safe_name(payload.name, "ZVOL 名称")
    parent_dataset = utils.require_safe_dataset(payload.parent_dataset, "父数据集")
    result = create_zvol_impl(pool, name, parent_dataset, payload.size,
                              payload.volblocksize, payload.compression, payload.sync, payload.sparse)
    result["article_defaults"] = ARTICLE_PROFILE["zvol_recommended"]
    return result


@app.delete("/api/zvols/{zvol_name:path}")
def delete_zvol(zvol_name: str):
    ensure_supported_runtime()
    full_name = utils.normalize_zvol_name(zvol_name)
    bound_backstores = [item for item in read_backstores() if item["zvol_name"] == full_name]
    if bound_backstores:
        names = ", ".join(item["name"] for item in bound_backstores)
        raise HTTPException(status_code=409, detail=f"ZVOL 仍绑定 backstore：{names}，请先删除 backstore 或 iSCSI target")
    return delete_zvol_impl(full_name)


@app.post("/api/zvols/{zvol_name:path}/snapshots")
def create_zvol_snapshot(zvol_name: str, payload: CreateSnapshotRequest):
    ensure_supported_runtime()
    return create_snapshot_impl(zvol_name, payload.snapshot_name)


@app.delete("/api/zvol-snapshots/{snapshot_name:path}")
def delete_zvol_snapshot(snapshot_name: str):
    ensure_supported_runtime()
    result = destroy_snapshot_impl(snapshot_name, promote_dependent_clones=True)
    return {"message": "ZVOL 快照删除成功", "snapshot_name": result["snapshot_name"], "promoted_clones": result["promoted_clones"]}


@app.post("/api/zvol-snapshots/{snapshot_name:path}/rollback")
def rollback_zvol_snapshot(snapshot_name: str):
    ensure_supported_runtime()
    return rollback_snapshot_impl(snapshot_name)


@app.post("/api/zvol-snapshots/{snapshot_name:path}/reverse-sync")
def reverse_sync_zvol_snapshot(snapshot_name: str, payload: ReverseSyncSnapshotRequest):
    ensure_supported_runtime()
    return reverse_sync_impl(snapshot_name, payload.base_snapshot)


@app.post("/api/zvol-snapshots/{snapshot_name:path}/sync-to-clones")
def sync_origin_snapshot_to_clones(snapshot_name: str, payload: SyncOriginSnapshotRequest):
    ensure_supported_runtime()
    requested = {utils.normalize_zvol_name(item) for item in payload.clone_names}
    return sync_origin_to_clones_impl(snapshot_name, requested)


@app.post("/api/zvols/clones")
def clone_zvol(payload: CloneZvolRequest):
    ensure_supported_runtime()
    pool = utils.require_safe_name(payload.pool, "存储池名称")
    parent_dataset = utils.require_safe_dataset(payload.parent_dataset, "父数据集")
    name = utils.require_safe_name(payload.name, "克隆名称")
    return clone_zvol_impl(payload.snapshot_name, pool, parent_dataset, name)


# ---- 定时快照任务 ----

@app.get("/api/snapshot-jobs")
def list_snapshot_jobs():
    ensure_supported_runtime()
    return {"jobs": list_snapshot_jobs_view()}


@app.post("/api/snapshot-jobs")
def create_snapshot_job(payload: SnapshotScheduleRequest):
    ensure_supported_runtime()
    jobs = read_snapshot_jobs()
    job = sanitize_snapshot_job(payload.model_dump())
    ensure_unique_snapshot_job(job, jobs)
    jobs.append(job)
    scheduler.write_snapshot_jobs(jobs)
    log_utils.append_log("info", "scheduler", "创建定时快照任务", {
        "object_type": "Job", "object_name": job["id"],
        "action": "create", "result": "success",
        "zvol_name": job["zvol_name"], "prefix": job["prefix"],
        "interval_minutes": job["interval_minutes"], "keep_count": job["keep_count"],
    })
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
    scheduler.write_snapshot_jobs(jobs)
    log_utils.append_log("info", "scheduler", "更新定时快照任务", {
        "object_type": "Job", "object_name": job_id,
        "action": "update", "result": "success",
    })
    return {"message": "定时快照任务已更新", "job": build_snapshot_job_view(updated_job)}


@app.delete("/api/snapshot-jobs/{job_id}")
def delete_snapshot_job(job_id: str):
    ensure_supported_runtime()
    jobs = read_snapshot_jobs()
    remaining = [job for job in jobs if job.get("id") != job_id]
    if len(remaining) == len(jobs):
        raise HTTPException(status_code=404, detail="定时快照任务不存在")
    scheduler.write_snapshot_jobs(remaining)
    log_utils.append_log("info", "scheduler", "删除定时快照任务", {
        "object_type": "Job", "object_name": job_id,
        "action": "delete", "result": "success",
    })
    return {"message": "定时快照任务已删除", "job_id": job_id}


# ---- 日志 ----

@app.get("/api/logs")
def list_logs(
    level: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=1000),
):
    return {
        "logs": log_utils.read_logs(level=level, limit=limit),
        "counts": log_utils.summarize_log_counts(),
        "levels": ["debug", "info", "warning", "error"],
    }


# ---- Backstore ----

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
    zvol_name = utils.normalize_zvol_name(payload.zvol_name)
    backstore_name = utils.require_safe_name(
        payload.backstore_name or utils.default_backstore_name(zvol_name),
        "Backstore 名称",
    )
    created = create_backstore_impl(zvol_name, backstore_name)
    return {"message": "Backstore 创建成功", "backstore": created}


@app.delete("/api/backstores/{backstore_name}")
def delete_backstore(backstore_name: str):
    ensure_supported_runtime()
    name = utils.require_safe_name(backstore_name, "Backstore 名称")
    return delete_backstore_impl(name)


# ---- iSCSI Target ----

@app.get("/api/iscsi/targets")
def list_iscsi_targets():
    ensure_supported_runtime()
    return {"targets": read_iscsi_targets()}


@app.post("/api/iscsi/targets")
def create_iscsi_target(payload: CreateIscsiTargetRequest):
    ensure_supported_runtime()
    iqn = utils.require_iqn(payload.iqn)
    if payload.tpg < 1:
        raise HTTPException(status_code=400, detail="TPG 必须大于等于 1")
    return create_iscsi_target_impl(iqn, payload.tpg)


@app.delete("/api/iscsi/targets/{iqn}")
def delete_iscsi_target(
    iqn: str,
    delete_backstore: bool = Query(default=False, description="删除 target 后顺带清理 backstore"),
):
    ensure_supported_runtime()
    iqn = utils.require_iqn(iqn)
    return delete_iscsi_target_impl(iqn, delete_backstore)


@app.post("/api/iscsi/targets/{iqn}/luns")
def create_iscsi_lun(iqn: str, payload: CreateIscsiLunRequest):
    ensure_supported_runtime()
    iqn = utils.require_iqn(iqn)
    backstore_name = utils.require_safe_name(payload.backstore_name, "Backstore 名称")
    if payload.tpg < 1:
        raise HTTPException(status_code=400, detail="TPG 必须大于等于 1")
    return create_iscsi_lun_impl(iqn, backstore_name, payload.tpg)


@app.get("/api/iscsi/targets/{iqn}/settings")
def get_iscsi_target_settings(iqn: str, tpg: int = Query(default=1)):
    ensure_supported_runtime()
    iqn = utils.require_iqn(iqn)
    tpg_dir = resolve_tpg_dir(iqn, tpg)
    return {"settings": read_tpg_settings(iqn, tpg_dir)}


@app.put("/api/iscsi/targets/{iqn}/settings")
def update_iscsi_target_settings(iqn: str, payload: TargetSettingsRequest):
    ensure_supported_runtime()
    iqn = utils.require_iqn(iqn)
    return update_target_settings_impl(iqn, payload)


@app.post("/api/iscsi/targets/{iqn}/portals")
def add_iscsi_portal(iqn: str, payload: PortalRequest):
    ensure_supported_runtime()
    iqn = utils.require_iqn(iqn)
    return add_portal_impl(iqn, payload.ip, payload.port, payload.tpg)


@app.delete("/api/iscsi/targets/{iqn}/portals")
def delete_iscsi_portal(
    iqn: str,
    ip: str = Query(...),
    port: int = Query(default=3260),
    tpg: int = Query(default=1),
):
    ensure_supported_runtime()
    iqn = utils.require_iqn(iqn)
    return delete_portal_impl(iqn, ip, port, tpg)


@app.post("/api/iscsi/targets/{iqn}/acls")
def add_iscsi_acl(iqn: str, payload: AclRequest):
    ensure_supported_runtime()
    iqn = utils.require_iqn(iqn)
    initiator_iqn = utils.require_iqn(payload.initiator_iqn, "Initiator IQN")
    return add_acl_impl(iqn, initiator_iqn, payload.tpg)


@app.delete("/api/iscsi/targets/{iqn}/acls")
def delete_iscsi_acl(
    iqn: str,
    initiator_iqn: str = Query(...),
    tpg: int = Query(default=1),
):
    ensure_supported_runtime()
    iqn = utils.require_iqn(iqn)
    initiator_iqn = utils.require_iqn(initiator_iqn, "Initiator IQN")
    return delete_acl_impl(iqn, initiator_iqn, tpg)


@app.put("/api/iscsi/targets/{iqn}/acls/{initiator_iqn}/chap")
def update_iscsi_acl_chap(iqn: str, initiator_iqn: str, payload: AclChapRequest):
    ensure_supported_runtime()
    iqn = utils.require_iqn(iqn)
    initiator_iqn = utils.require_iqn(initiator_iqn, "Initiator IQN")
    return update_acl_chap_impl(iqn, initiator_iqn, payload)


# ---- 启动事件 ----

@app.on_event("startup")
def startup_snapshot_jobs_worker():
    utils.ensure_state_dir(STATE_DIR)
    start_scheduler_worker()


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
