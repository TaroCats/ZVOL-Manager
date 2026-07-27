#!/usr/bin/env python3
"""
ZFS ZVOL Manager Backend — FastAPI application.

Provides REST API for managing ZFS ZVOLs: create, list, delete,
mount/unmount, snapshot, clone, rollback, and iSCSI export operations.
All ZFS operations are performed via subprocess calls to zfs/zpool/targetcli.
"""

import asyncio
import json
import os
import re
import subprocess
import time
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
APP_VERSION = "1.3.0"
app = FastAPI(title="ZVOL Manager", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).parent / "frontend"
LOG_DIR = Path(os.environ.get("TRIM_PKGVAR", "/tmp"))
LOG_FILE = LOG_DIR / "zvol-manager.log"


# ---------------------------------------------------------------------------
# Audit log (destructive operations only)
# ---------------------------------------------------------------------------
def _audit(action: str, target: str, **extra) -> None:
    """Append a structured audit log line for destructive ops."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "action": action,
            "target": target,
            **extra,
        }
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[AUDIT] " + json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Concurrency: per-zvol lock map to prevent destructive races
# ---------------------------------------------------------------------------
_ZVOL_LOCKS: dict[str, asyncio.Lock] = {}
_LOCKS_GUARD = asyncio.Lock()


async def _zvol_lock(name: str) -> asyncio.Lock:
    async with _LOCKS_GUARD:
        lock = _ZVOL_LOCKS.get(name)
        if lock is None:
            lock = asyncio.Lock()
            _ZVOL_LOCKS[name] = lock
        return lock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 30) -> str:
    """Run a command and return stdout. Raise HTTPException on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"Command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail=f"Command timed out: {' '.join(cmd)}")

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(result.stderr or result.stdout or "").strip() or "command failed",
        )
    return result.stdout


def _run_rc(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command, return CompletedProcess (no exception on non-zero exit)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def _run_ok(cmd: list[str], timeout: int = 30) -> bool:
    """Run a command, return True if exit code == 0."""
    return _run_rc(cmd, timeout=timeout).returncode == 0


def _is_safe_name(name: str) -> bool:
    """Validate zvol / pool / snapshot names to prevent injection."""
    return bool(re.fullmatch(r"[a-zA-Z0-9_./:@+\-]+", name))


def _safe(val: str) -> str:
    """Strip whitespace from ZFS CLI output fields."""
    return val.strip()


# ---------------------------------------------------------------------------
# API — Version
# ---------------------------------------------------------------------------
@app.get("/api/version")
def get_version():
    return {"version": APP_VERSION}


# ---------------------------------------------------------------------------
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
    filesystem: str = "ext4"           # ext4 (default) | xfs | btrfs
    force_format: bool = False         # explicit confirmation to overwrite existing FS


class UnmountRequest(BaseModel):
    force: bool = False                # ignore "device busy" warnings


class SnapshotRequest(BaseModel):
    snapshot_name: str


class RollbackRequest(BaseModel):
    snapshot: str
    force: bool = False                # -r (destroy newer snapshots) requires explicit opt-in


class CloneRequest(BaseModel):
    snapshot: str
    clone_name: str
    target_pool: str


class IscsiCreateRequest(BaseModel):
    initiator_name: Optional[str] = None  # optional: restrict to specific initiator IQN


class IscsiAclRequest(BaseModel):
    initiator_iqn: str


class IscsiSettingsRequest(BaseModel):
    portal_ip: Optional[str] = None
    portal_port: Optional[int] = None
    chap_enabled: Optional[bool] = None
    chap_userid: Optional[str] = None
    chap_password: Optional[str] = None
    mutual_userid: Optional[str] = None
    mutual_password: Optional[str] = None
    mutual_disabled: Optional[bool] = None  # explicit "clear mutual CHAP"


# ---------------------------------------------------------------------------
# Mountpoint validation
# ---------------------------------------------------------------------------
def _validate_mountpoint(mp: str) -> str:
    """Validate mountpoint: must be absolute, normalized, not a system dir."""
    if not mp:
        raise HTTPException(status_code=400, detail="mountpoint 不能为空")
    if "\x00" in mp:
        raise HTTPException(status_code=400, detail="mountpoint 包含非法字符")
    p = Path(mp)
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="mountpoint 必须是绝对路径")
    try:
        resolved = p.resolve(strict=False)
    except Exception:
        raise HTTPException(status_code=400, detail="mountpoint 路径无法解析")
    # Block mounting over critical system paths.  We use basenames since
    # resolve() may rewrite leading dirs (e.g. /etc → /private/etc on macOS).
    forbidden_components = {"bin", "sbin", "etc", "boot", "proc", "sys", "dev", "var", "usr"}
    forbidden_exact = {"/"}
    if str(resolved) in forbidden_exact:
        raise HTTPException(status_code=400, detail=f"不允许挂载到系统根目录")
    for part in resolved.parts:
        if part in (os.sep, "/"):
            continue
        if part in forbidden_components:
            raise HTTPException(
                status_code=400,
                detail=f"不允许挂载到系统目录: {part}",
            )
    if str(resolved).startswith(("/proc/", "/sys/", "/dev/")):
        raise HTTPException(status_code=400, detail=f"不允许挂载到内核/设备目录: {resolved}")
    return str(resolved)


# ---------------------------------------------------------------------------
# API — ZVOLs (create / delete)
# ---------------------------------------------------------------------------
@app.post("/api/zvols")
async def create_zvol(req: CreateZvolRequest):
    """Create a new ZVOL."""
    if not _is_safe_name(req.pool) or not _is_safe_name(req.name):
        raise HTTPException(status_code=400, detail="Invalid pool or name characters")

    full_name = f"{req.pool}/{req.name}"

    lock = await _zvol_lock(full_name)
    async with lock:
        # Check if already exists (use rc-based helper so we only catch 'not found')
        check = _run_rc(["zfs", "list", "-H", "-o", "name", full_name])
        if check.returncode == 0:
            raise HTTPException(status_code=409, detail=f"ZVOL already exists: {full_name}")
        if check.returncode != 1 and "does not exist" not in (check.stderr or "").lower():
            raise HTTPException(
                status_code=500,
                detail=(check.stderr or check.stdout or "zfs list failed").strip(),
            )

        flags: list[str] = []
        if req.sparse:
            flags.append("-s")
        flags.extend(["-V", req.size])
        flags.extend(["-o", f"volblocksize={req.volblocksize}"])
        flags.extend(["-o", f"compression={req.compression}"])

        _run(["zfs", "create", *flags, full_name])
        _audit("create_zvol", full_name, size=req.size, volblocksize=req.volblocksize,
               compression=req.compression, sparse=req.sparse)
    return {"status": "ok", "name": full_name}


@app.delete("/api/zvols/{name:path}")
async def delete_zvol(name: str, force: bool = Query(False)):
    """Delete a ZVOL.  WARNING: irreversible."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    lock = await _zvol_lock(name)
    async with lock:
        return _delete_zvol_impl(name, force)


def _delete_zvol_impl(name: str, force: bool):
    # If a targetcli backstore references this zvol, refuse unless force=true.
    if _iscsi_zvol_to_target(name) is not None:
        if not force:
            raise HTTPException(
                status_code=409,
                detail=(f"ZVOL {name} 仍被 iSCSI 导出引用，请先删除 iSCSI 目标，"
                        f"或显式传入 ?force=true"),
            )
        # force=true → tear down the iSCSI target first (no lock: caller holds it)
        _delete_iscsi_target_unlocked(name, raised_on_missing=False)

    # Try to unmount (best effort; if busy and not force, surface error)
    try:
        mounted = _run(["zfs", "get", "-H", "-o", "value", "mounted", name]).strip()
    except HTTPException:
        mounted = "unknown"
    if mounted == "yes":
        # Check if any process holds the device
        dev_path = f"/dev/zvol/{name}"
        holders = _run_rc(["fuser", "-m", dev_path])
        if holders.returncode == 0 and holders.stdout.strip():
            if not force:
                raise HTTPException(
                    status_code=409,
                    detail=(f"ZVOL {name} 正被进程占用: {holders.stdout.strip()}，"
                            f"请先停止相关进程或传入 ?force=true"),
                )
        # Try graceful unmount
        um = _run_rc(["zfs", "unmount", name])
        if um.returncode != 0:
            um2 = _run_rc(["umount", dev_path])
            if um2.returncode != 0 and not force:
                raise HTTPException(
                    status_code=500,
                    detail=f"卸载失败: {(um.stderr or um2.stderr).strip()}",
                )

    _run(["zfs", "destroy", name])
    _audit("delete_zvol", name, force=force)
    return {"status": "ok", "name": name}


# ---------------------------------------------------------------------------
# API — Mount / Unmount
# ---------------------------------------------------------------------------
def _probe_filesystem(dev_path: str) -> Optional[str]:
    """Return the filesystem type on dev_path, or None if no filesystem."""
    res = _run_rc(["blkid", "-o", "value", "-s", "TYPE", dev_path])
    if res.returncode == 0:
        return (res.stdout or "").strip() or None
    return None


@app.post("/api/zvols/{name:path}/mount")
async def mount_zvol(name: str, req: MountRequest):
    """Format zvol (if not already) and mount it.

    Safety: refuses to overwrite an existing non-empty filesystem unless
    ``force_format=true`` is explicitly set. Refuses unknown filesystems too.
    """
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")
    if req.filesystem not in {"ext4", "xfs", "btrfs"}:
        raise HTTPException(status_code=400, detail=f"不支持的文件系统: {req.filesystem}")

    mountpoint = _validate_mountpoint(req.mountpoint)

    lock = await _zvol_lock(name)
    async with lock:
        return _mount_zvol_impl(name, mountpoint, req.filesystem, req.force_format)


def _mount_zvol_impl(name: str, mountpoint: str, filesystem: str, force_format: bool):
    dev_path = f"/dev/zvol/{name}"

    # Device must exist
    if not os.path.exists(dev_path):
        raise HTTPException(status_code=500, detail=f"设备节点不存在: {dev_path}")

    # Already mounted? → return current mountpoint
    try:
        if _run(["zfs", "get", "-H", "-o", "value", "mounted", name]).strip() == "yes":
            mp = _run(["zfs", "get", "-H", "-o", "value", "mountpoint", name]).strip()
            return {"status": "ok", "mountpoint": mp, "note": "Already mounted"}
    except HTTPException:
        pass

    # Probe existing filesystem
    existing_fs = _probe_filesystem(dev_path)
    if existing_fs and existing_fs != filesystem:
        if not force_format:
            raise HTTPException(
                status_code=409,
                detail=(f"ZVOL 上已存在 {existing_fs} 文件系统，如确认要覆盖为 {filesystem}，"
                        f"请显式传 force_format=true"),
            )
        # Wipe existing signature so mkfs doesn't complain
        _run_ok(["wipefs", "-a", dev_path])

    if not existing_fs or existing_fs != filesystem:
        # Format
        if filesystem == "ext4":
            mkfs = _run_rc(["mkfs.ext4", "-F", dev_path], timeout=300)
        elif filesystem == "xfs":
            mkfs = _run_rc(["mkfs.xfs", "-f", dev_path], timeout=300)
        elif filesystem == "btrfs":
            mkfs = _run_rc(["mkfs.btrfs", "-f", dev_path], timeout=300)
        else:  # unreachable, guarded above
            raise HTTPException(status_code=400, detail="unsupported filesystem")
        if mkfs.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=(mkfs.stderr or mkfs.stdout or "mkfs failed").strip(),
            )

    # Create mountpoint directory and mount
    try:
        Path(mountpoint).mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise HTTPException(status_code=500, detail=f"无法创建挂载点: {e}")

    mnt = _run_rc(["mount", dev_path, mountpoint])
    if mnt.returncode != 0:
        raise HTTPException(status_code=500, detail=(mnt.stderr or "").strip() or "mount failed")

    # Persist in fstab so it survives reboots
    try:
        _write_fstab(dev_path, mountpoint, filesystem)
    except Exception as e:  # non-fatal
        _audit("mount_zvol_fstab_failed", name, error=str(e))

    _audit("mount_zvol", name, mountpoint=mountpoint, filesystem=filesystem,
           force_format=force_format)
    return {"status": "ok", "mountpoint": mountpoint, "filesystem": filesystem}


def _write_fstab(dev_path: str, mountpoint: str, fstype: str) -> None:
    """Append (or replace) an fstab entry for dev_path."""
    fstab = Path("/etc/fstab")
    if not fstab.exists():
        return
    text = fstab.read_text()
    lines = [ln for ln in text.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")
             and not _fstab_matches(ln, dev_path, mountpoint)]
    lines.append(f"{dev_path}\t{mountpoint}\t{fstype}\tdefaults,nofail\t0\t0")
    fstab.write_text("\n".join(lines) + "\n")


def _fstab_matches(line: str, dev_path: str, mountpoint: str) -> bool:
    parts = line.split()
    if len(parts) < 2:
        return False
    return parts[0] == dev_path or parts[1] == mountpoint


@app.post("/api/zvols/{name:path}/unmount")
async def unmount_zvol(name: str, req: UnmountRequest):
    """Unmount a ZVOL."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    lock = await _zvol_lock(name)
    async with lock:
        return _unmount_zvol_impl(name, req.force)


def _unmount_zvol_impl(name: str, force: bool):
    dev_path = f"/dev/zvol/{name}"
    last_err = ""
    um1 = _run_rc(["zfs", "unmount", name])
    if um1.returncode == 0:
        _remove_fstab_entry(dev_path)
        _audit("unmount_zvol", name)
        return {"status": "ok"}
    last_err = (um1.stderr or "").strip()

    um2 = _run_rc(["umount", dev_path])
    if um2.returncode == 0:
        _remove_fstab_entry(dev_path)
        _audit("unmount_zvol", name)
        return {"status": "ok"}
    last_err = last_err or (um2.stderr or "").strip()

    if not force:
        raise HTTPException(
            status_code=500,
            detail=f"卸载失败: {last_err or 'device busy'}（如确认可强制，请传 force=true）",
        )
    lazy = _run_rc(["umount", "-l", dev_path])
    if lazy.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"强制卸载失败: {(lazy.stderr or '').strip()}",
        )
    _remove_fstab_entry(dev_path)
    _audit("unmount_zvol", name, force=True)
    return {"status": "ok", "note": "lazy unmount"}


def _remove_fstab_entry(dev_path: str) -> None:
    """Remove the fstab line for dev_path if present."""
    fstab = Path("/etc/fstab")
    if not fstab.exists():
        return
    try:
        text = fstab.read_text()
        lines = [ln for ln in text.splitlines()
                 if not _fstab_matches(ln, dev_path, "")]
        fstab.write_text("\n".join(lines) + ("\n" if lines else ""))
    except Exception as e:
        _audit("unmount_fstab_failed", dev_path, error=str(e))


# ---------------------------------------------------------------------------
# API — Snapshots
# ---------------------------------------------------------------------------
def _snapshot_clones(snap_full: str) -> list[str]:
    """Return list of clone names that depend on the given snapshot."""
    out = _run_rc(["zfs", "list", "-H", "-t", "volume", "-o", "name,origin"])
    clones: list[str] = []
    if out.returncode != 0:
        return clones
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        origin = parts[1].strip()
        # origin is the snapshot the clone was created from, prefixed with the dataset path
        if origin.endswith(f"@{snap_full.split('@', 1)[-1]}") or origin == snap_full:
            clones.append(parts[0].strip())
    return clones


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
async def create_snapshot(name: str, req: SnapshotRequest):
    """Create a snapshot of a ZVOL."""
    if not _is_safe_name(name) or not _is_safe_name(req.snapshot_name):
        raise HTTPException(status_code=400, detail="Invalid name characters")
    if "@" in req.snapshot_name:
        raise HTTPException(status_code=400, detail="snapshot_name 不能包含 @")

    snap_full = f"{name}@{req.snapshot_name}"
    lock = await _zvol_lock(name)
    async with lock:
        _run(["zfs", "snapshot", snap_full])
        _audit("create_snapshot", snap_full)
    return {"status": "ok", "snapshot": snap_full}


@app.delete("/api/zvols/{name:path}/snapshots/{snap}")
async def delete_snapshot(name: str, snap: str, force: bool = Query(False)):
    """Delete a snapshot. Refuses if any clone still depends on it."""
    if not _is_safe_name(name) or not _is_safe_name(snap):
        raise HTTPException(status_code=400, detail="Invalid name characters")
    if "@" in snap:
        raise HTTPException(status_code=400, detail="snap 参数不能包含 @")

    snap_full = f"{name}@{snap}"
    lock = await _zvol_lock(name)
    async with lock:
        dependents = _snapshot_clones(snap_full)
        if dependents and not force:
            raise HTTPException(
                status_code=409,
                detail=(f"快照 {snap_full} 被以下克隆依赖: {', '.join(dependents)}。"
                        f"如确认请先删除依赖克隆，或传 ?force=true"),
            )
        _run(["zfs", "destroy", snap_full])
        _audit("delete_snapshot", snap_full, dependents=dependents, force=force)
    return {"status": "ok", "snapshot": snap_full}


@app.post("/api/zvols/{name:path}/rollback")
async def rollback_snapshot(name: str, req: RollbackRequest):
    """Rollback a ZVOL to a specified snapshot.  WARNING: destroys newer data.

    The ZFS ``-r`` flag recursively destroys any newer snapshots; the client
    must opt in via ``force=true`` to authorize that.
    """
    if not _is_safe_name(name) or not _is_safe_name(req.snapshot):
        raise HTTPException(status_code=400, detail="Invalid name characters")

    snap_full = f"{name}@{req.snapshot}"
    lock = await _zvol_lock(name)
    async with lock:
        # If a targetcli backstore references this zvol, refuse.
        if _iscsi_zvol_to_target(name) is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"ZVOL {name} 仍被 iSCSI 导出，回滚会导致 initiator IO 错误，"
                        f"请先删除 iSCSI 目标"),
            )

        if req.force:
            _run(["zfs", "rollback", "-r", snap_full])
            _audit("rollback_zvol", snap_full, force=True, recursive=True)
        else:
            rc = _run_rc(["zfs", "rollback", snap_full])
            if rc.returncode != 0:
                err = (rc.stderr or "").lower()
                if "newer snapshots" in err or "has more recent" in err:
                    raise HTTPException(
                        status_code=409,
                        detail=("存在更新的快照，回滚需要 force=true（-r 会一并销毁它们）"),
                    )
                raise HTTPException(status_code=500, detail=(rc.stderr or rc.stdout).strip())
            _audit("rollback_zvol", snap_full, force=False, recursive=False)
    return {"status": "ok", "snapshot": snap_full}


# ---------------------------------------------------------------------------
# API — Clone
# ---------------------------------------------------------------------------
@app.post("/api/zvols/{name:path}/clone")
async def clone_zvol(name: str, req: CloneRequest):
    """Clone a ZVOL from a snapshot."""
    if not _is_safe_name(name) or not _is_safe_name(req.snapshot) or not _is_safe_name(req.clone_name) or not _is_safe_name(req.target_pool):
        raise HTTPException(status_code=400, detail="Invalid name characters")
    if "@" in req.snapshot:
        raise HTTPException(status_code=400, detail="snapshot 参数不能包含 @")

    snap_full = f"{name}@{req.snapshot}"
    clone_full = f"{req.target_pool}/{req.clone_name}"

    # Lock on the source dataset so we don't race with delete/rollback.
    lock = await _zvol_lock(name)
    async with lock:
        check = _run_rc(["zfs", "list", "-H", "-o", "name", clone_full])
        if check.returncode == 0:
            raise HTTPException(status_code=409, detail=f"目标已存在: {clone_full}")
        if check.returncode not in (0, 1) and "does not exist" not in (check.stderr or "").lower():
            raise HTTPException(status_code=500, detail=(check.stderr or check.stdout).strip())

        _run(["zfs", "clone", snap_full, clone_full])
        _audit("clone_zvol", clone_full, source=snap_full)
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


def _iscsi_saveconfig_with_rollback(steps: list[list[str]], target_iqn: str, zvol_name: str) -> None:
    """Run saveconfig and, on failure, run the inverse of `steps` to undo."""
    res = _run_rc(["targetcli", "saveconfig"])
    if res.returncode == 0:
        return
    # Rollback: each step is assumed to be a creation, so the inverse is a delete in reverse.
    _run_ok(["targetcli", "iscsi", "delete", target_iqn])
    _run_ok(["targetcli", "backstores/block", "delete", zvol_name])
    raise HTTPException(
        status_code=500,
        detail=f"targetcli saveconfig 失败: {(res.stderr or res.stdout).strip()}（已自动回滚）",
    )


# ---------------------------------------------------------------------------
# API — iSCSI targets
# ---------------------------------------------------------------------------
@app.get("/api/iscsi/service-status")
def iscsi_service_status():
    """Report LIO / targetcli availability and suggest where to do CHAP/Portal/ACL.

    The actual management UI for CHAP / Portal / ACL lives in fnOS's own
    iSCSI service page.  This endpoint just tells the frontend whether the
    LIO backend (targetcli + target_core_mod) is operational.
    """
    has_targetcli = _run_ok(["which", "targetcli"])
    module_loaded = False
    try:
        with open("/proc/modules") as f:
            module_loaded = "target_core_mod" in f.read()
    except FileNotFoundError:
        module_loaded = None  # macOS / dev box

    # Try the standard iSCSI port — if anything is listening, link out is safe.
    portal_listening = _run_rc(["bash", "-c", "ss -tln 2>/dev/null | grep -q ':3260 ' || netstat -tln 2>/dev/null | grep -q ':3260 '"])
    portal_active = portal_listening.returncode == 0

    return {
        "targetcli_installed": has_targetcli,
        "lio_module_loaded": module_loaded,
        "portal_active": portal_active,
        "manage_url": "/fnos/iscsi",
        "manage_note": "CHAP/Portal/ACL 等详细配置请到 fnOS 系统设置 → iSCSI 服务",
    }


@app.get("/api/iscsi/targets")
def list_iscsi_targets():
    """List all iSCSI targets backed by ZVOLs."""
    _targetcli_available()

    targets = []
    # Single deep listing (3 levels: iqn/tpg1/lun, iqn/tpg1/acls, iqn/tpg1/portals).
    try:
        deep_out = _run(["targetcli", "ls", "/iscsi", "depth=3"])
    except HTTPException:
        deep_out = ""

    if not deep_out.strip():
        return {"targets": targets}

    # Walk the tree; state machine collects context as we encounter portals/acls/lun.
    iqns_in_order: list[str] = []
    iqn_zvol: dict[str, str] = {}
    iqn_portal: dict[str, str] = {}
    iqn_acls: dict[str, list[str]] = {}
    iqn_chap: dict[str, bool] = {}

    current_iqn: Optional[str] = None
    section: Optional[str] = None
    auth_flag: Optional[bool] = None

    for raw in deep_out.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        # Top-level iSCSI list line: "o- iqn.xxxxxx ..."
        m_iqn = re.search(r"^\|?o-\s+(iqn\.[^\s]+)", stripped)
        if m_iqn and "/iscsi/" not in stripped and "tpg1" not in stripped:
            current_iqn = m_iqn.group(1)
            iqns_in_order.append(current_iqn)
            iqn_acls.setdefault(current_iqn, [])
            section = None
            continue
        if not current_iqn:
            continue

        # Section detection
        if "tpg1" in stripped and "portals" in stripped:
            section = "portals"
            continue
        if "tpg1" in stripped and "acls" in stripped:
            section = "acls"
            continue
        if "tpg1" in stripped and "lun" in stripped and "luns" not in stripped:
            section = "lun"
            continue
        if "authentication" in stripped:
            m_auth = re.search(r"authentication=(\d)", stripped)
            if m_auth:
                iqn_chap[current_iqn] = m_auth.group(1) == "1"
            continue

        if section == "portals":
            mp = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", stripped)
            if mp and current_iqn not in iqn_portal:
                iqn_portal[current_iqn] = f"{mp.group(1)}:{mp.group(2)}"
        elif section == "acls":
            ma = re.search(r"(iqn\.[^\s]+)", stripped)
            if ma and stripped.startswith("o-") and ma.group(1) not in iqn_acls[current_iqn]:
                iqn_acls[current_iqn].append(ma.group(1))
        elif section == "lun":
            mz = re.search(r"/dev/zvol/(\S+)", stripped)
            if mz and current_iqn not in iqn_zvol:
                iqn_zvol[current_iqn] = mz.group(1).strip()

    for iqn in iqns_in_order:
        zvol_name = iqn_zvol.get(iqn)
        if not zvol_name:
            continue  # only show ZVOL-backed targets
        targets.append({
            "iqn": iqn,
            "zvol": zvol_name,
            "portal": iqn_portal.get(iqn, "0.0.0.0:3260"),
            "acls": iqn_acls.get(iqn, []),
            "chap_enabled": iqn_chap.get(iqn, False),
        })

    return {"targets": targets}


@app.post("/api/zvols/{name:path}/iscsi")
async def create_iscsi_target(name: str, req: IscsiCreateRequest):
    """Create an iSCSI target backed by a ZVOL."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid zvol name")

    _targetcli_available()

    try:
        _run(["zfs", "list", "-H", "-o", "name", name])
    except HTTPException:
        raise HTTPException(status_code=404, detail=f"ZVOL not found: {name}")

    if _iscsi_zvol_to_target(name):
        raise HTTPException(status_code=409, detail=f"iSCSI target already exists for {name}")

    target_iqn = _iscsi_target_name_for(name)
    dev_path = f"/dev/zvol/{name}"

    lock = await _zvol_lock(name)
    async with lock:
        # 1. backstore
        res = _run_rc(["targetcli", "backstores/block", "create", f"name={name}", f"dev={dev_path}"])
        if res.returncode != 0:
            raise HTTPException(status_code=500, detail=(res.stderr or res.stdout).strip())

        # 2. iSCSI target
        res = _run_rc(["targetcli", "iscsi", "create", f"wwn={target_iqn}"])
        if res.returncode != 0:
            _run_ok(["targetcli", "backstores/block", "delete", name])
            raise HTTPException(status_code=500, detail=(res.stderr or res.stdout).strip())

        # 3. LUN
        res = _run_rc([
            "targetcli", f"/iscsi/{target_iqn}/tpg1/lun", "create",
            f"/backstores/block/{name}", "0",
        ])
        if res.returncode != 0:
            _run_ok(["targetcli", "iscsi", "delete", target_iqn])
            _run_ok(["targetcli", "backstores/block", "delete", name])
            raise HTTPException(status_code=500, detail=(res.stderr or res.stdout).strip())

        # 4. ACL
        if req.initiator_name:
            if not re.match(r"^iqn\.", req.initiator_name):
                _run_ok(["targetcli", "iscsi", "delete", target_iqn])
                _run_ok(["targetcli", "backstores/block", "delete", name])
                raise HTTPException(status_code=400, detail="initiator_name 必须以 iqn. 开头")
            res = _run_rc([
                "targetcli", f"/iscsi/{target_iqn}/tpg1/acls", "create",
                req.initiator_name,
            ])
            if res.returncode != 0:
                _run_ok(["targetcli", "iscsi", "delete", target_iqn])
                _run_ok(["targetcli", "backstores/block", "delete", name])
                raise HTTPException(status_code=500, detail=(res.stderr or res.stdout).strip())

        # 5. saveconfig + rollback on failure
        _iscsi_saveconfig_with_rollback([], target_iqn, name)

        _audit("create_iscsi_target", name, iqn=target_iqn, initiator=req.initiator_name or "")

    return {
        "status": "ok",
        "iqn": target_iqn,
        "portal": "0.0.0.0:3260",
        "zvol": name,
        "initiator": req.initiator_name or "",
    }


@app.delete("/api/zvols/{name:path}/iscsi")
async def delete_iscsi_target(name: str):
    """Remove the iSCSI target associated with a ZVOL."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    _targetcli_available()

    target_iqn = _iscsi_target_name_for(name)

    # Serialize iSCSI mutations on the same zvol/target.
    lock = await _zvol_lock(name)
    async with lock:
        return _delete_iscsi_target_unlocked(name, raised_on_missing=True)


def _delete_iscsi_target_unlocked(name: str, raised_on_missing: bool):
    """Core iSCSI delete.  Assumes the per-zvol lock is already held by the
    caller.  Use this from inside other locked operations to avoid
    asyncio.Lock self-deadlock.
    """
    target_iqn = _iscsi_target_name_for(name)
    target_ok = _run_ok(["targetcli", "iscsi", "delete", target_iqn])
    block_ok = _run_ok(["targetcli", "backstores/block", "delete", name])

    save = _run_rc(["targetcli", "saveconfig"])
    save_ok = save.returncode == 0

    if not target_ok and not block_ok:
        if raised_on_missing:
            raise HTTPException(status_code=404, detail=f"No iSCSI target for {name}")
        # Caller chose to ignore "not found" (delete_zvol with force).
        return {"status": "ok", "iqn": target_iqn, "zvol": name, "note": "no target"}

    err: list[str] = []
    if not target_ok:
        err.append(f"targetcli iscsi delete 失败")
    if not block_ok:
        err.append(f"targetcli backstores/block delete 失败（残留 backstore 需手动清理）")
    if not save_ok:
        err.append(f"saveconfig 失败: {(save.stderr or save.stdout).strip()}")

    _audit("delete_iscsi_target", name, iqn=target_iqn,
           target_ok=target_ok, block_ok=block_ok, save_ok=save_ok)

    if err:
        return {"status": "partial", "iqn": target_iqn, "zvol": name, "warnings": err}
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
    acls: list[str] = []
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
            "chap_enabled": _iscsi_chap_enabled(target_iqn),
        }
    }


@app.post("/api/zvols/{name:path}/iscsi/acl")
async def add_iscsi_acl(name: str, req: IscsiAclRequest):
    """Add an initiator IQN to the ACL list."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    _targetcli_available()

    if not req.initiator_iqn.startswith("iqn."):
        raise HTTPException(status_code=400, detail="initiator_iqn 必须以 iqn. 开头")

    if not _iscsi_zvol_to_target(name):
        raise HTTPException(status_code=404, detail=f"No iSCSI target for {name}")

    target_iqn = _iscsi_target_name_for(name)
    lock = await _zvol_lock(name)
    async with lock:
        res = _run_rc([
            "targetcli", f"/iscsi/{target_iqn}/tpg1/acls", "create",
            req.initiator_iqn,
        ])
        if res.returncode != 0:
            raise HTTPException(status_code=500, detail=(res.stderr or res.stdout).strip())
        save = _run_rc(["targetcli", "saveconfig"])
        if save.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"已加入 ACL，但 saveconfig 失败: {(save.stderr or save.stdout).strip()}",
            )
        _audit("add_iscsi_acl", name, iqn=target_iqn, initiator=req.initiator_iqn)
    return {"status": "ok", "iqn": req.initiator_iqn}


@app.delete("/api/zvols/{name:path}/iscsi/acl")
async def remove_iscsi_acl(name: str, initiator_iqn: str = Query(...)):
    """Remove an initiator IQN from the ACL list."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    _targetcli_available()

    if not initiator_iqn.startswith("iqn."):
        raise HTTPException(status_code=400, detail="initiator_iqn 必须以 iqn. 开头")

    if not _iscsi_zvol_to_target(name):
        raise HTTPException(status_code=404, detail=f"No iSCSI target for {name}")

    target_iqn = _iscsi_target_name_for(name)
    lock = await _zvol_lock(name)
    async with lock:
        res = _run_rc([
            "targetcli", f"/iscsi/{target_iqn}/tpg1/acls", "delete",
            initiator_iqn,
        ])
        if res.returncode != 0:
            raise HTTPException(status_code=500, detail=(res.stderr or res.stdout).strip())
        save = _run_rc(["targetcli", "saveconfig"])
        if save.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"已删除 ACL，但 saveconfig 失败: {(save.stderr or save.stdout).strip()}",
            )
        _audit("remove_iscsi_acl", name, iqn=target_iqn, initiator=initiator_iqn)
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

    portal_ip, portal_port = "0.0.0.0", 3260
    try:
        portal_out = _run(["targetcli", "ls", f"{base}/portals"])
        m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", portal_out)
        if m:
            portal_ip, portal_port = m.group(1), int(m.group(2))
    except HTTPException:
        pass

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
async def update_iscsi_settings(name: str, req: IscsiSettingsRequest):
    """Update iSCSI target settings: portal IP/port, CHAP credentials."""
    if not _is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid name")

    _targetcli_available()

    info = _iscsi_zvol_to_target(name)
    if not info:
        raise HTTPException(status_code=404, detail=f"No iSCSI target for {name}")

    target_iqn = info["iqn"]
    base = f"/iscsi/{target_iqn}/tpg1"
    lock = await _zvol_lock(name)
    async with lock:
        return _update_iscsi_settings_impl(name, target_iqn, base, req)


def _update_iscsi_settings_impl(
    name: str, target_iqn: str, base: str, req: IscsiSettingsRequest
):
    # --- Portal ---
    if req.portal_ip is not None or req.portal_port is not None:
        if req.portal_ip is not None and not re.match(r"^\d+\.\d+\.\d+\.\d+$", req.portal_ip):
            raise HTTPException(status_code=400, detail="portal_ip 必须是合法 IPv4 地址")
        if req.portal_port is not None and not (1 <= int(req.portal_port) <= 65535):
            raise HTTPException(status_code=400, detail="portal_port 必须在 1-65535")

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

        if (new_ip, new_port) != (old_ip, old_port):
            _run_ok(["targetcli", f"{base}/portals", "delete",
                     f"ip_address={old_ip}", f"ip_port={old_port}"])
            res = _run_rc(["targetcli", f"{base}/portals", "create",
                           f"ip_address={new_ip}", f"ip_port={new_port}"])
            if res.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"新建 portal 失败: {(res.stderr or res.stdout).strip()}",
                )

    # --- CHAP ---
    if req.chap_enabled is not None:
        if req.chap_enabled:
            if not req.chap_userid or not req.chap_password:
                raise HTTPException(
                    status_code=400, detail="启用 CHAP 需要提供 userid 和 password",
                )
            _run(["targetcli", f"{base}", "set", "attribute", "authentication=1"])
            res = _run_rc([
                "targetcli", f"{base}", "set", "auth",
                f"userid={req.chap_userid}", f"password={req.chap_password}",
            ])
            if res.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"设置 CHAP 凭据失败: {(res.stderr or res.stdout).strip()}",
                )
        else:
            # Disable CHAP: turn off the attribute and clear credentials safely.
            _run(["targetcli", f"{base}", "set", "attribute", "authentication=0"])
            _run_ok(["targetcli", f"{base}", "set", "auth", "userid="])
            _run_ok(["targetcli", f"{base}", "set", "auth", "password="])
            _run_ok(["targetcli", f"{base}", "set", "auth", "mutual_userid="])
            _run_ok(["targetcli", f"{base}", "set", "auth", "mutual_password="])
    elif req.chap_userid is not None and req.chap_password is not None:
        res = _run_rc([
            "targetcli", f"{base}", "set", "auth",
            f"userid={req.chap_userid}", f"password={req.chap_password}",
        ])
        if res.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"更新 CHAP 凭据失败: {(res.stderr or res.stdout).strip()}",
            )

    # --- Mutual CHAP ---
    if req.mutual_disabled:
        # Explicitly clear mutual CHAP. targetcli 在新版本可接受空值，
        # 旧版本会报错 — 失败不致命（CHAP 仍可用）。
        _run_ok(["targetcli", f"{base}", "set", "auth", "mutual_userid="])
        _run_ok(["targetcli", f"{base}", "set", "auth", "mutual_password="])
    elif req.mutual_userid is not None and req.mutual_password is not None:
        res = _run_rc([
            "targetcli", f"{base}", "set", "auth",
            f"mutual_userid={req.mutual_userid}", f"mutual_password={req.mutual_password}",
        ])
        if res.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"设置 Mutual CHAP 失败: {(res.stderr or res.stdout).strip()}",
            )

    save = _run_rc(["targetcli", "saveconfig"])
    if save.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"saveconfig 失败: {(save.stderr or save.stdout).strip()}",
        )

    _audit("update_iscsi_settings", name, iqn=target_iqn,
           portal=f"{req.portal_ip}:{req.portal_port}",
           chap_enabled=req.chap_enabled)
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
