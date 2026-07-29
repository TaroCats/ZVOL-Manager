"""iSCSI/LIO 操作：Backstore、Target、LUN、Portal、ACL"""

import os
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from server.utils import (
    CONFIGFS_TARGET_ROOT,
    CONFIGFS_TARGET_CORE,
    CONFIGFS_ISCSI,
    RESERVED_ISCSI_NAMES,
    read_text_if_exists,
    parse_portal_name,
    run_cmd,
    run_result,
    zvol_device_path,
    default_backstore_name,
    targetcli_saveconfig,
    targetcli_tpg_path,
)


def read_backstores() -> list[dict]:
    backstores: list[dict] = []
    if not CONFIGFS_TARGET_CORE or not CONFIGFS_TARGET_CORE.exists():
        return backstores
    try:
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
                backstores.append({
                    "name": entry.name,
                    "device": device,
                    "enabled": enabled,
                    "serial": serial,
                    "zvol_name": zvol_name,
                    "iblock_path": str(entry),
                    "iblock_group": iblock_dir.name,
                })
    except Exception:
        pass
    return backstores


def get_backstore(name: str) -> Optional[dict]:
    for item in read_backstores():
        if item["name"] == name:
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
    run_cmd(["targetcli", "/backstores/block", "create", f"name={backstore_name}", f"dev={device}"], timeout=120)
    created = get_backstore(backstore_name)
    if not created:
        raise HTTPException(status_code=500, detail="Backstore 创建命令已执行，但未在 configfs 中找到结果")
    targetcli_saveconfig()
    return created


def delete_backstore_impl(name: str) -> dict:
    if not get_backstore(name):
        raise HTTPException(status_code=404, detail="Backstore 不存在")
    if backstore_is_used(name):
        raise HTTPException(status_code=409, detail="Backstore 仍被 iSCSI target 使用，请先删除 target")
    run_cmd(["targetcli", "/backstores/block", "delete", name], timeout=120)
    if get_backstore(name):
        raise HTTPException(status_code=500, detail="Backstore 删除后重新扫描仍存在，请到 fnOS 上进一步排查")
    targetcli_saveconfig()
    return {"message": "Backstore 删除成功", "backstore_name": name}


# ---- iSCSI Target ----

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
    return {"initiator_iqn": acl_item.name, **auth}


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
    if not CONFIGFS_ISCSI or not CONFIGFS_ISCSI.exists():
        return targets
    try:
        backstores_by_name = {item["name"]: item for item in read_backstores()}
    except Exception:
        backstores_by_name = {}
    try:
        iqn_dirs = sorted(CONFIGFS_ISCSI.iterdir())
    except Exception:
        return targets
    for iqn_dir in iqn_dirs:
        if not iqn_dir.is_dir():
            continue
        if iqn_dir.name in RESERVED_ISCSI_NAMES:
            continue
        try:
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
                tpg_items.append({
                    "name": tpg_dir.name,
                    "luns": lun_items,
                    "portals": portals,
                    "acls": acl_items,
                    "settings": read_tpg_settings(iqn_dir.name, tpg_dir),
                })
            zvol_names = []
            for backstore_name in sorted(used_backstores):
                backstore = backstores_by_name.get(backstore_name)
                if backstore and backstore["zvol_name"]:
                    zvol_names.append(backstore["zvol_name"])
            targets.append({
                "iqn": iqn_dir.name,
                "tpgs": tpg_items,
                "backstores": sorted(used_backstores),
                "zvol_names": zvol_names,
                "portals": sorted(target_portals),
                "acl_names": [item["initiator_iqn"] for item in target_acls],
                "acls": target_acls,
                "settings": tpg_items[0]["settings"] if tpg_items else None,
            })
        except Exception:
            continue
    return targets


def get_target(iqn: str) -> Optional[dict]:
    for item in read_iscsi_targets():
        if item["iqn"] == iqn:
            return item
    return None


def create_iscsi_target_impl(iqn: str, tpg: int) -> dict:
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
    return {"message": "iSCSI Target 创建成功", "target": target}


def delete_iscsi_target_impl(iqn: str, delete_backstore: bool) -> dict:
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
    return {"message": "iSCSI Target 删除成功", "iqn": iqn, "deleted_backstores": deleted_backstores}


def create_iscsi_lun_impl(iqn: str, backstore_name: str, tpg: int) -> dict:
    target = get_target(iqn)
    if not target:
        raise HTTPException(status_code=404, detail="iSCSI Target 不存在")
    if not get_backstore(backstore_name):
        raise HTTPException(status_code=404, detail="Backstore 不存在")
    if backstore_name in target["backstores"]:
        raise HTTPException(status_code=409, detail="该 Backstore 已绑定到当前 Target")
    tpg_path = targetcli_tpg_path(iqn, tpg)
    run_cmd(["targetcli", f"{tpg_path}/luns", "create", f"/backstores/block/{backstore_name}"], timeout=120)
    targetcli_saveconfig()
    updated_target = get_target(iqn)
    if not updated_target or backstore_name not in updated_target["backstores"]:
        raise HTTPException(status_code=500, detail="LUN 创建命令已执行，但重新扫描 target 状态时未发现该 backstore")
    return {"message": "LUN 创建成功", "target": updated_target, "backstore_name": backstore_name}


def update_target_settings_impl(iqn: str, payload: "TargetSettingsRequest") -> dict:
    from server.models import TargetSettingsRequest
    tpg_path = targetcli_tpg_path(iqn, payload.tpg)
    if payload.authentication is not None:
        run_cmd(["targetcli", tpg_path, "set", "attribute", f"authentication={1 if payload.authentication else 0}"], timeout=120)
    if payload.generate_node_acls is not None:
        run_cmd(["targetcli", tpg_path, "set", "attribute", f"generate_node_acls={1 if payload.generate_node_acls else 0}"], timeout=120)
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


def add_portal_impl(iqn: str, ip: str, port: int, tpg: int) -> dict:
    tpg_path = targetcli_tpg_path(iqn, tpg)
    run_cmd(["targetcli", f"{tpg_path}/portals", "create", ip.strip(), str(port)], timeout=120)
    targetcli_saveconfig()
    return {"message": "Portal 创建成功", "target": get_target(iqn)}


def delete_portal_impl(iqn: str, ip: str, port: int, tpg: int) -> dict:
    tpg_path = targetcli_tpg_path(iqn, tpg)
    run_cmd(["targetcli", f"{tpg_path}/portals", "delete", ip.strip(), str(port)], timeout=120)
    targetcli_saveconfig()
    return {"message": "Portal 删除成功", "target": get_target(iqn)}


def add_acl_impl(iqn: str, initiator_iqn: str, tpg: int) -> dict:
    tpg_path = targetcli_tpg_path(iqn, tpg)
    run_cmd(["targetcli", f"{tpg_path}/acls", "create", initiator_iqn], timeout=120)
    targetcli_saveconfig()
    return {"message": "ACL 创建成功", "target": get_target(iqn)}


def delete_acl_impl(iqn: str, initiator_iqn: str, tpg: int) -> dict:
    tpg_path = targetcli_tpg_path(iqn, tpg)
    run_cmd(["targetcli", f"{tpg_path}/acls", "delete", initiator_iqn], timeout=120)
    targetcli_saveconfig()
    return {"message": "ACL 删除成功", "target": get_target(iqn)}


def update_acl_chap_impl(iqn: str, initiator_iqn: str, payload: "AclChapRequest") -> dict:
    from server.models import AclChapRequest
    tpg_path = targetcli_tpg_path(iqn, payload.tpg)
    auth_updates = [f"userid={payload.userid}", f"password={payload.password}"]
    if payload.mutual_userid is not None:
        auth_updates.append(f"mutual_userid={payload.mutual_userid}")
    if payload.mutual_password is not None:
        auth_updates.append(f"mutual_password={payload.mutual_password}")
    run_cmd(["targetcli", f"{tpg_path}/acls/{initiator_iqn}", "set", "auth", *auth_updates], timeout=120)
    targetcli_saveconfig()
    return {"message": "ACL CHAP 已更新", "target": get_target(iqn)}
