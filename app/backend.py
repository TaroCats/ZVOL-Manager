#!/usr/bin/env python3
"""
ZFS ZVOL Manager Backend — FastAPI application.

Provides REST API for managing ZFS ZVOLs: create, list, delete,
mount/unmount, snapshot, clone, and rollback operations.
All ZFS operations are performed via subprocess calls to zfs/zpool.
"""

import json
import re
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="ZVOL Manager", version="1.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).parent / "frontend"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 30) -> str:
    """Run a command and return stdout. Raise HTTPException on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"Command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail=f"Command timed out: {' '.join(cmd)}")

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _run_ok(cmd: list[str], timeout: int = 60) -> bool:
    """Run a command, return True if exit code == 0."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0
    except Exception:
        return False


def _is_safe_name(name: str) -> bool:
    """Validate zvol / pool / snapshot names to prevent injection."""
    return bool(re.fullmatch(r'[a-zA-Z0-9_./:@+\-]+', name))


def _safe(val: str) -> str:
    """Strip whitespace from ZFS CLI output fields."""
    return val.strip()


def _list_dir(path: str) -> list[str]:
    """List directory contents, empty list if not exists."""
    try:
        return sorted(Path(path).iterdir())
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# API — Version
@app.get("/api/version")
def get_version():
    return {"version": app.version}

# API — Pools
# ---------------------------------------------------------------------------
@app.get("/api/pools")
def list_pools():
    """List all ZFS storage pools."""
    out = _run(["zpool", "list", "-H", "-o", "name"])
    pools = [line.strip() for line in out.strip().splitlines() if line.strip()]
    return {"pools": pools}


# ---------------------------------------------------------------------------
# API — ZVOLs (list)
# ---------------------------------------------------------------------------
@app.get("/api/zvols")
def list_zvols():
    """List all ZVOLs with key properties."""
    props = "name,volsize,used,refer,compressratio,volblocksize,mounted,mountpoint"
    try:
        out = _run(["zfs", "list", "-H", "-t", "volume", "-o", props])
    except HTTPException as e:
        if "no datasets available" in (e.detail or "").lower():
            return {"zvols": []}
        raise

    zvols = []
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        name = _safe(parts[0])
        zvol = {
            "name": name,
            "pool": name.split("/")[0] if "/" in name else "",
            "volsize": _safe(parts[1]) if len(parts) > 1 else "-",
            "used": _safe(parts[2]) if len(parts) > 2 else "-",
            "refer": _safe(parts[3]) if len(parts) > 3 else "-",
            "compressratio": _safe(parts[4]) if len(parts) > 4 else "-",
            "volblocksize": _safe(parts[5]) if len(parts) > 5 else "-",
            "mounted": _safe(parts[6]) if len(parts) > 6 else "-",
            "mountpoint": _safe(parts[7]) if len(parts) > 7 else "-",
        }
        zvols.append(zvol)

    return {"zvols": zvols}


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------
class CreateZvolRequest(BaseModel):
    pool: str
    name: str
    size: str                          # e.g. "10G", "500M"
    volblocksize: str = "16K"
    compression: str = "lz4"
    sparse: bool = True


class MountRequest(BaseModel):
    mountpoint: str


class SnapshotRequest(BaseModel):
    snapshot_name: str


class RollbackRequest(BaseModel):
    snapshot: str


class CloneRequest(BaseModel):
    snapshot: str
    clone_name: str
    target_pool: str


class IscsiCreateRequest(BaseModel):
    target_name: Optional[str] = None     # 可选：自定义 IQN 后缀，留空自动生成
    portal_port: int = 3260
    initiator_name: Optional[str] = None  # 可选：限制访问的 initiator IQN


class IscsiAclRequest(BaseModel):
    initiator_iqn: str                   # initiator 的 IQN，如 iqn.2025-01.com.client:client1


class IscsiSettingsRequest(BaseModel):
    portal_ip: Optional[str] = None        # 监听 IP，如 "0.0.0.0" 或 "192.168.1.100"
    portal_port: Optional[int] = None      # 监听端口，默认 3260
    chap_enabled: Optional[bool] = None    # 是否启用 CHAP 认证
    chap_userid: Optional[str] = None      # CHAP 用户名
    chap_password: Optional[str] = None    # CHAP 密码
    mutual_userid: Optional[str] = None    # 双向 CHAP（target 端认证 initiator）
    mutual_password: Optional[str] = None  # 双向 CHAP 密码


# ---------------------------------------------------------------------------
# API — ZVOLs (create / delete)
# ---------------------------------------------------------------------------
@app.post("/api/zvols")
def create_zvol(req: CreateZvolRequest):
    """Create a new ZVOL."""
    if not _is_safe_name(req.pool) or not _is_safe_name(req.name):
        raise HTTPException(status_code=400, detail="Invalid pool or name characters")

    full_name = f"{req.pool}/{req.name}"

    # Check if already exists
    try:
        _run(["zfs", "list", "-H", "-o", "name", full_name])
        raise HTTPException(status_code=409, detail=f"ZVOL already exists: {full_name}")
    except HTTPException as e:
        if e.status_code != 500:
            raise

    # Build command
    flags = []
    if req.sparse:
        flags.append("-s")
    flags.extend(["-V", req.size])
    flags.extend(["-o", f"volblocksize={req.volblocksize}"])
    flags.extend(["-o", f"compression={req.compression}"])

    _run(["zfs", "create", *flags, full_name])
    return {"status": "ok", "name": full_name}


@app.delete("/api/zvols/{name:path}")
def delete_zvol(name: str):
    """Delete a ZVOL.  WARNING: irreversible."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    # Unmount first if mounted
    _run_ok(["zfs", "unmount", name])

    _run(["zfs", "destroy", name])
    return {"status": "ok", "name": name}


# ---------------------------------------------------------------------------
# API — Mount / Unmount
# ---------------------------------------------------------------------------
@app.post("/api/zvols/{name:path}/mount")
def mount_zvol(name: str, req: MountRequest):
    """Format zvol as ext4 (if not already) and mount it."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    dev_path = f"/dev/zvol/{name}"

    # Check if already mounted
    try:
        out = _run(["zfs", "get", "-H", "-o", "value", "mounted", name])
        if out.strip() == "yes":
            # Already mounted — check if it has a filesystem
            mp = _run(["zfs", "get", "-H", "-o", "value", "mountpoint", name]).strip()
            return {"status": "ok", "mountpoint": mp, "note": "Already mounted"}
    except HTTPException:
        pass

    # Format as ext4 if not already (check with blkid)
    blkid = _run(["blkid", dev_path])
    if "ext4" not in blkid:
        _run(["mkfs.ext4", "-F", dev_path])

    # Create mountpoint directory and mount
    Path(req.mountpoint).mkdir(parents=True, exist_ok=True)
    _run(["mount", dev_path, req.mountpoint])
    return {"status": "ok", "mountpoint": req.mountpoint}


@app.post("/api/zvols/{name:path}/unmount")
def unmount_zvol(name: str):
    """Unmount a ZVOL."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    dev_path = f"/dev/zvol/{name}"

    # Try using zfs unmount first
    try:
        _run(["zfs", "unmount", name])
    except HTTPException:
        # Fallback to system umount
        _run(["umount", dev_path])

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# API — Snapshots
# ---------------------------------------------------------------------------
@app.get("/api/zvols/{name:path}/snapshots")
def list_snapshots(name: str):
    """List snapshots for a ZVOL."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    try:
        out = _run([
            "zfs", "list", "-H", "-t", "snapshot",
            "-o", "name,creation,used,refer",
            "-d", "1", name
        ])
    except HTTPException as e:
        if "no datasets available" in (e.detail or "").lower():
            return {"snapshots": []}
        raise

    snaps = []
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        full = _safe(parts[0])
        short = full.split("@")[-1] if "@" in full else full
        snaps.append({
            "name": full,
            "short_name": short,
            "creation": _safe(parts[1]),
            "used": _safe(parts[2]),
            "refer": _safe(parts[3]),
        })

    return {"snapshots": snaps}


@app.post("/api/zvols/{name:path}/snapshot")
def create_snapshot(name: str, req: SnapshotRequest):
    """Create a snapshot of a ZVOL."""
    if not _is_safe_name(name) or not _is_safe_name(req.snapshot_name):
        raise HTTPException(status_code=400, detail="Invalid name characters")

    snap_full = f"{name}@{req.snapshot_name}"
    _run(["zfs", "snapshot", snap_full])
    return {"status": "ok", "snapshot": snap_full}


@app.delete("/api/zvols/{name:path}/snapshots/{snap}")
def delete_snapshot(name: str, snap: str):
    """Delete a snapshot."""
    if not _is_safe_name(name) or not _is_safe_name(snap):
        raise HTTPException(status_code=400, detail="Invalid name characters")

    snap_full = f"{name}@{snap}"
    _run(["zfs", "destroy", snap_full])
    return {"status": "ok", "snapshot": snap_full}


@app.post("/api/zvols/{name:path}/rollback")
def rollback_snapshot(name: str, req: RollbackRequest):
    """Rollback a ZVOL to a specified snapshot.  WARNING: destroys newer data."""
    if not _is_safe_name(name) or not _is_safe_name(req.snapshot):
        raise HTTPException(status_code=400, detail="Invalid name characters")

    snap_full = f"{name}@{req.snapshot}"
    _run(["zfs", "rollback", "-r", snap_full])
    return {"status": "ok", "snapshot": snap_full}


# ---------------------------------------------------------------------------
# API — Clone
# ---------------------------------------------------------------------------
@app.post("/api/zvols/{name:path}/clone")
def clone_zvol(name: str, req: CloneRequest):
    """Clone a ZVOL from a snapshot."""
    if not _is_safe_name(name) or not _is_safe_name(req.snapshot) or not _is_safe_name(req.clone_name) or not _is_safe_name(req.target_pool):
        raise HTTPException(status_code=400, detail="Invalid name characters")

    snap_full = f"{name}@{req.snapshot}"
    clone_full = f"{req.target_pool}/{req.clone_name}"

    _run(["zfs", "clone", snap_full, clone_full])
    return {"status": "ok", "clone": clone_full, "source": snap_full}


# ---------------------------------------------------------------------------
# iSCSI helpers (Linux LIO / targetcli)
# ---------------------------------------------------------------------------
ISCSI_BASE_IQN = "iqn.2025-01.com.fnos.zvolmanager"


def _targetcli_available() -> None:
    """Raise 503 if targetcli is not installed."""
    if not _run_ok(["which", "targetcli"]):
        raise HTTPException(
            status_code=503,
            detail="targetcli 未安装，请先在宿主系统安装: apt install targetcli-fb",
        )


def _iscsi_target_name_for(zvol_name: str) -> str:
    """Map zvol name -> iSCSI target IQN, deterministic."""
    safe = re.sub(r"[^a-zA-Z0-9.\-:]", "-", zvol_name)
    return f"{ISCSI_BASE_IQN}:{safe}"


def _iscsi_chap_enabled(iqn: str) -> bool:
    """Check if CHAP authentication is enabled for an iSCSI target."""
    try:
        auth_val = _run(["targetcli", "get", f"/iscsi/{iqn}/tpg1", "attribute", "authentication"])
        return "authentication=1" in auth_val
    except HTTPException:
        return False


def _iscsi_zvol_to_target(zvol_name: str) -> Optional[dict]:
    """Look up existing iSCSI target backed by a zvol, return None if not found."""
    target_iqn = _iscsi_target_name_for(zvol_name)
    try:
        out = _run([
            "targetcli", "ls", f"/iscsi/{target_iqn}", "backstores/block"
        ])
    except HTTPException:
        return None
    if "No such path" in out or "does not exist" in out or not out.strip():
        return None
    return {
        "iqn": target_iqn,
        "zvol": zvol_name,
        "dev": f"/dev/zvol/{zvol_name}",
    }


# ---------------------------------------------------------------------------
# API — iSCSI targets
# ---------------------------------------------------------------------------
@app.get("/api/iscsi/targets")
def list_iscsi_targets():
    """List all iSCSI targets backed by ZVOLs."""
    _targetcli_available()

    targets = []
    # 列举 /backstores/block 下由 zvol 创建的 backstore
    try:
        bs_out = _run([
            "targetcli", "ls", "/backstores/block", "name"
        ])
    except HTTPException:
        bs_out = ""

    # 通过 /iscsi 列举所有 target，看哪些 wwn 关联了 block 后端
    try:
        iscsi_out = _run(["targetcli", "ls", "/iscsi"])
    except HTTPException:
        iscsi_out = ""

    # 解析 iqn 列表
    iqns = []
    for line in iscsi_out.splitlines():
        m = re.search(r"iqn\.[^\s]+", line)
        if m:
            iqns.append(m.group(0))

    for iqn in iqns:
        # 看此 target 是否有 lun
        try:
            lun_out = _run(["targetcli", "ls", f"/iscsi/{iqn}/tpg1/lun"])
        except HTTPException:
            continue
        if "No LUN" in lun_out or not lun_out.strip():
            continue
        # 从 lun 输出里找 /dev/zvol/...
        m = re.search(r"/dev/zvol/(\S+)", lun_out)
        if not m:
            continue
        zvol_name = m.group(1).strip()

        # acl
        acl_out = _run_ok(["targetcli", "ls", f"/iscsi/{iqn}/tpg1/acls"])
        acls = []
        if acl_out:
            try:
                acl_text = _run(["targetcli", "ls", f"/iscsi/{iqn}/tpg1/acls"])
                acls = re.findall(r"iqn\.[^\s]+", acl_text)
            except HTTPException:
                pass

        # 门户
        portal_out = ""
        try:
            portal_out = _run(["targetcli", "ls", f"/iscsi/{iqn}/tpg1/portals"])
        except HTTPException:
            pass
        portal = "0.0.0.0:3260"
        m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", portal_out)
        if m:
            portal = f"{m.group(1)}:{m.group(2)}"

        targets.append({
            "iqn": iqn,
            "zvol": zvol_name,
            "portal": portal,
            "acls": acls,
            "chap_enabled": _iscsi_chap_enabled(iqn),
        })

    return {"targets": targets}


@app.post("/api/zvols/{name:path}/iscsi")
def create_iscsi_target(name: str, req: IscsiCreateRequest):
    """Create an iSCSI target backed by a ZVOL."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid zvol name")

    _targetcli_available()

    # 确认 zvol 存在
    try:
        _run(["zfs", "list", "-H", "-o", "name", name])
    except HTTPException:
        raise HTTPException(status_code=404, detail=f"ZVOL not found: {name}")

    # 已存在则直接返回
    if _iscsi_zvol_to_target(name):
        raise HTTPException(status_code=409, detail=f"iSCSI target already exists for {name}")

    target_iqn = _iscsi_target_name_for(name)
    dev_path = f"/dev/zvol/{name}"

    # 1. 创建 backstore (block 类型)
    _run([
        "targetcli", "backstores/block", "create",
        f"name={name}", f"dev={dev_path}"
    ])

    # 2. 创建 iSCSI target
    _run(["targetcli", "iscsi", "create", f"wwn={target_iqn}"])

    # 3. 创建 LUN，关联到 block backstore
    try:
        _run([
            "targetcli", f"/iscsi/{target_iqn}/tpg1/lun", "create",
            f"/backstores/block/{name}", "0"
        ])
    except HTTPException as e:
        # 回滚
        _run_ok(["targetcli", "iscsi", "delete", target_iqn])
        _run_ok(["targetcli", "backstores/block", "delete", name])
        raise e

    # 4. 可选 ACL 限制
    if req.initiator_name:
        if not re.match(r"^iqn\.", req.initiator_name):
            # 回滚
            _run_ok(["targetcli", "iscsi", "delete", target_iqn])
            _run_ok(["targetcli", "backstores/block", "delete", name])
            raise HTTPException(status_code=400, detail="initiator_name 必须以 iqn. 开头")
        try:
            _run([
                "targetcli", f"/iscsi/{target_iqn}/tpg1/acls", "create",
                req.initiator_name
            ])
        except HTTPException as e:
            _run_ok(["targetcli", "iscsi", "delete", target_iqn])
            _run_ok(["targetcli", "backstores/block", "delete", name])
            raise e

    # 5. 写入 firewall-friendly portal（0.0.0.0:3260 默认即监听所有网卡，仅显式调整时设置）
    # 默认配置已监听所有网卡 3260，不做改动

    # 保存配置
    _run_ok(["targetcli", "saveconfig"])

    return {
        "status": "ok",
        "iqn": target_iqn,
        "portal": f"0.0.0.0:{req.portal_port}",
        "zvol": name,
        "initiator": req.initiator_name or "",
    }


@app.delete("/api/zvols/{name:path}/iscsi")
def delete_iscsi_target(name: str):
    """Remove the iSCSI target associated with a ZVOL."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    _targetcli_available()

    target_iqn = _iscsi_target_name_for(name)

    # 先删 target（含其下所有 lun 和 acls），再删 backstore
    target_ok = _run_ok(["targetcli", "iscsi", "delete", target_iqn])
    block_ok = _run_ok(["targetcli", "backstores/block", "delete", name])

    _run_ok(["targetcli", "saveconfig"])

    if not target_ok and not block_ok:
        raise HTTPException(status_code=404, detail=f"No iSCSI target for {name}")

    return {"status": "ok", "iqn": target_iqn, "zvol": name}


@app.get("/api/zvols/{name:path}/iscsi")
def get_iscsi_target(name: str):
    """Get iSCSI target details for a specific zvol."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    _targetcli_available()

    info = _iscsi_zvol_to_target(name)
    if not info:
        return {"target": None}

    target_iqn = info["iqn"]
    acls = []
    try:
        acl_text = _run(["targetcli", "ls", f"/iscsi/{target_iqn}/tpg1/acls"])
        acls = re.findall(r"iqn\.[^\s]+", acl_text)
    except HTTPException:
        pass

    portal = "0.0.0.0:3260"
    try:
        portal_out = _run(["targetcli", "ls", f"/iscsi/{target_iqn}/tpg1/portals"])
        m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", portal_out)
        if m:
            portal = f"{m.group(1)}:{m.group(2)}"
    except HTTPException:
        pass

    return {
        "target": {
            "iqn": target_iqn,
            "zvol": name,
            "portal": portal,
            "acls": acls,
            "dev": info["dev"],
        }
    }


@app.post("/api/zvols/{name:path}/iscsi/acl")
def add_iscsi_acl(name: str, req: IscsiAclRequest):
    """Add an initiator IQN to the ACL list."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    _targetcli_available()

    if not req.initiator_iqn.startswith("iqn."):
        raise HTTPException(status_code=400, detail="initiator_iqn 必须以 iqn. 开头")

    if not _iscsi_zvol_to_target(name):
        raise HTTPException(status_code=404, detail=f"No iSCSI target for {name}")

    target_iqn = _iscsi_target_name_for(name)
    _run([
        "targetcli", f"/iscsi/{target_iqn}/tpg1/acls", "create",
        req.initiator_iqn
    ])
    _run_ok(["targetcli", "saveconfig"])
    return {"status": "ok", "iqn": req.initiator_iqn}


@app.delete("/api/zvols/{name:path}/iscsi/acl")
def remove_iscsi_acl(name: str, initiator_iqn: str = Query(...)):
    """Remove an initiator IQN from the ACL list."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    _targetcli_available()

    if not initiator_iqn.startswith("iqn."):
        raise HTTPException(status_code=400, detail="initiator_iqn 必须以 iqn. 开头")

    if not _iscsi_zvol_to_target(name):
        raise HTTPException(status_code=404, detail=f"No iSCSI target for {name}")

    target_iqn = _iscsi_target_name_for(name)
    _run([
        "targetcli", f"/iscsi/{target_iqn}/tpg1/acls", "delete",
        initiator_iqn
    ])
    _run_ok(["targetcli", "saveconfig"])
    return {"status": "ok", "iqn": initiator_iqn}


@app.get("/api/zvols/{name:path}/iscsi/settings")
def get_iscsi_settings(name: str):
    """Get iSCSI target settings: portal, CHAP auth status."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    _targetcli_available()

    info = _iscsi_zvol_to_target(name)
    if not info:
        raise HTTPException(status_code=404, detail=f"No iSCSI target for {name}")

    target_iqn = info["iqn"]
    base = f"/iscsi/{target_iqn}/tpg1"

    # Portal
    portal_ip, portal_port = "0.0.0.0", 3260
    try:
        portal_out = _run(["targetcli", "ls", f"{base}/portals"])
        m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", portal_out)
        if m:
            portal_ip, portal_port = m.group(1), int(m.group(2))
    except HTTPException:
        pass

    # CHAP
    chap_enabled = False
    chap_userid = ""
    try:
        auth_val = _run(["targetcli", "get", f"{base}", "attribute", "authentication"])
        chap_enabled = "authentication=1" in auth_val
        userid_out = _run(["targetcli", "get", f"{base}", "auth", "userid"])
        m = re.search(r"userid=(.*)", userid_out)
        if m:
            chap_userid = m.group(1).strip()
    except HTTPException:
        pass

    # Mutual CHAP
    mutual_userid = ""
    try:
        out = _run(["targetcli", "get", f"{base}", "auth", "mutual_userid"])
        m = re.search(r"mutual_userid=(.*)", out)
        if m:
            mutual_userid = m.group(1).strip()
    except HTTPException:
        pass

    return {
        "portal_ip": portal_ip,
        "portal_port": portal_port,
        "chap_enabled": chap_enabled,
        "chap_userid": chap_userid,
        "mutual_userid": mutual_userid,
    }


@app.put("/api/zvols/{name:path}/iscsi/settings")
def update_iscsi_settings(name: str, req: IscsiSettingsRequest):
    """Update iSCSI target settings: portal IP/port, CHAP credentials."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    _targetcli_available()

    info = _iscsi_zvol_to_target(name)
    if not info:
        raise HTTPException(status_code=404, detail=f"No iSCSI target for {name}")

    target_iqn = info["iqn"]
    base = f"/iscsi/{target_iqn}/tpg1"

    # --- Portal ---
    if req.portal_ip is not None or req.portal_port is not None:
        # 先获取当前 portal 以便删除
        old_ip, old_port = "0.0.0.0", 3260
        try:
            portal_out = _run(["targetcli", "ls", f"{base}/portals"])
            m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", portal_out)
            if m:
                old_ip, old_port = m.group(1), int(m.group(2))
        except HTTPException:
            pass

        new_ip = req.portal_ip if req.portal_ip is not None else old_ip
        new_port = req.portal_port if req.portal_port is not None else old_port

        # 删除旧 portal、创建新 portal
        _run_ok(["targetcli", f"{base}/portals", "delete", f"ip_address={old_ip}", f"ip_port={old_port}"])
        _run(["targetcli", f"{base}/portals", "create", f"ip_address={new_ip}", f"ip_port={new_port}"])

    # --- CHAP ---
    if req.chap_enabled is not None:
        if req.chap_enabled:
            if not req.chap_userid or not req.chap_password:
                raise HTTPException(status_code=400, detail="启用 CHAP 需要提供 userid 和 password")
            _run(["targetcli", f"{base}", "set", "attribute", "authentication=1"])
            _run(["targetcli", f"{base}", "set", "auth", f"userid={req.chap_userid}", f"password={req.chap_password}"])
        else:
            _run(["targetcli", f"{base}", "set", "attribute", "authentication=0"])
            _run_ok(["targetcli", f"{base}", "set", "auth", "userid=", "password="])

    elif req.chap_userid is not None and req.chap_password is not None:
        # 仅更新凭据（CHAP 已启用时）
        _run(["targetcli", f"{base}", "set", "auth", f"userid={req.chap_userid}", f"password={req.chap_password}"])

    # --- Mutual CHAP ---
    if req.mutual_userid is not None and req.mutual_password is not None:
        _run(["targetcli", f"{base}", "set", "auth", f"mutual_userid={req.mutual_userid}", f"mutual_password={req.mutual_password}"])

    _run_ok(["targetcli", "saveconfig"])
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Static file serving (must be AFTER API routes)
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    """Serve frontend HTML entry point."""
    return FileResponse(FRONTEND_DIR / "index.html")


# Mount frontend static files
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
