#!/usr/bin/env python3

import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


APP_VERSION = "1.2.0"
ROOT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT_DIR / "frontend"
CONFIGFS_TARGET_ROOT = Path("/sys/kernel/config/target")
CONFIGFS_TARGET_CORE = CONFIGFS_TARGET_ROOT / "core"
CONFIGFS_ISCSI = CONFIGFS_TARGET_ROOT / "iscsi"
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")
SAFE_DATASET_RE = re.compile(r"^[A-Za-z0-9_.:+/-]+$")
SAFE_IQN_RE = re.compile(r"^[A-Za-z0-9.:_-]+$")

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


class CreateIscsiTargetRequest(BaseModel):
    zvol_name: str
    iqn: str = Field(description="例如 iqn.2026-07.local.fnos:steam")
    backstore_name: Optional[str] = None
    initiator_iqn: Optional[str] = None
    auto_create_backstore: bool = True
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


def ensure_supported_runtime() -> None:
    if platform.system().lower() != "linux":
        raise HTTPException(status_code=503, detail="当前不是 Linux/fnOS 环境，ZFS 与 LIO 功能不可用")
    if not CONFIGFS_TARGET_ROOT.exists():
        raise HTTPException(status_code=503, detail="未检测到 /sys/kernel/config/target，LIO 功能不可用")


def run_cmd(cmd: list[str], timeout: int = 30) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"命令不存在：{cmd[0]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail=f"命令超时：{' '.join(cmd)}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "命令执行失败"
        raise HTTPException(status_code=500, detail=detail)
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


def read_iscsi_targets() -> list[dict]:
    targets: list[dict] = []
    if not CONFIGFS_ISCSI.exists():
        return targets

    backstores_by_name = {item["name"]: item for item in read_backstores()}
    for iqn_dir in sorted(CONFIGFS_ISCSI.iterdir()):
        if not iqn_dir.is_dir():
            continue

        tpg_items = []
        used_backstores: set[str] = set()
        target_portals: set[str] = set()
        target_acls: list[dict] = []

        for tpg_dir in iter_tpg_dirs(iqn_dir):
            lun_items = []
            portals: list[dict] = []
            acl_items: list[dict] = []
            luns_dir = tpg_dir / "luns"
            if luns_dir.exists():
                for lun_dir in sorted(luns_dir.iterdir()):
                    if not lun_dir.is_dir():
                        continue
                    linked_backstores = []
                    for port_link in sorted(lun_dir.iterdir()):
                        if not port_link.is_symlink():
                            continue
                        real_path = os.path.realpath(port_link)
                        backstore_name = Path(real_path).name
                        if backstore_name:
                            used_backstores.add(backstore_name)
                            linked_backstores.append(backstore_name)
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
    output = run_cmd(
        [
            "zfs",
            "list",
            "-H",
            "-t",
            "volume",
            "-o",
            "name,volsize,used,refer,compressratio,volblocksize,compression,sync,referreservation",
        ]
    )

    backstores = {item["zvol_name"]: item for item in read_backstores()}
    targets = read_iscsi_targets()
    iqn_by_backstore: dict[str, list[str]] = {}
    for target in targets:
        for backstore_name in target["backstores"]:
            iqn_by_backstore.setdefault(backstore_name, []).append(target["iqn"])

    zvols = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 9:
            continue
        name = parts[0]
        backstore = backstores.get(name)
        target_iqns = []
        if backstore:
            target_iqns = iqn_by_backstore.get(backstore["name"], [])
        zvols.append(
            {
                "name": name,
                "device": zvol_device_path(name),
                "volsize": parts[1],
                "used": parts[2],
                "refer": parts[3],
                "compressratio": parts[4],
                "volblocksize": parts[5],
                "compression": parts[6],
                "sync": parts[7],
                "refreservation": parts[8],
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
    zvol_name = normalize_zvol_name(payload.zvol_name)
    iqn = require_iqn(payload.iqn)
    backstore_name = require_safe_name(
        payload.backstore_name or default_backstore_name(zvol_name),
        "Backstore 名称",
    )
    initiator_iqn = require_iqn(payload.initiator_iqn, "Initiator IQN") if payload.initiator_iqn else None
    if payload.tpg < 1:
        raise HTTPException(status_code=400, detail="TPG 必须大于等于 1")

    created_backstore = False
    if not get_backstore(backstore_name):
        if not payload.auto_create_backstore:
            raise HTTPException(status_code=404, detail="Backstore 不存在，请先创建或勾选自动创建")
        create_backstore_impl(zvol_name, backstore_name)
        created_backstore = True

    if get_target(iqn):
        raise HTTPException(status_code=409, detail="iSCSI Target 已存在")

    tpg_path = f"/iscsi/{iqn}/tpg{payload.tpg}"
    try:
        run_cmd(["targetcli", "/iscsi", "create", iqn], timeout=120)
        run_cmd(
            [
                "targetcli",
                f"{tpg_path}/luns",
                "create",
                f"/backstores/block/{backstore_name}",
            ],
            timeout=120,
        )
        if initiator_iqn:
            run_cmd(["targetcli", f"{tpg_path}/acls", "create", initiator_iqn], timeout=120)
    except HTTPException:
        if get_target(iqn):
            run_result(["targetcli", "/iscsi", "delete", iqn], timeout=120)
        if created_backstore and get_backstore(backstore_name) and not backstore_is_used(backstore_name):
            run_result(["targetcli", "/backstores/block", "delete", backstore_name], timeout=120)
        raise

    target = get_target(iqn)
    if not target:
        raise HTTPException(status_code=500, detail="Target 创建命令已执行，但未在 configfs 中找到结果")
    targetcli_saveconfig()
    return {
        "message": "iSCSI Target 创建成功",
        "target": target,
        "backstore_name": backstore_name,
    }


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


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
