"""ZFS 操作：ZVOL、快照、克隆、send/recv"""

import subprocess
from typing import Optional

from fastapi import HTTPException

from server.log_utils import append_log
from server.utils import (
    normalize_snapshot_name,
    normalize_zvol_name,
    require_safe_name,
    run_cmd,
    run_result,
    zvol_device_path,
)


def get_zfs_property(dataset: str, prop: str, default: str = "-") -> str:
    result = run_result(["zfs", "get", "-H", "-o", "value", prop, dataset], timeout=30)
    if result.returncode != 0:
        return default
    value = (result.stdout or "").strip()
    return value or default


def ensure_parent_dataset(full_parent: str) -> None:
    probe = run_result(["zfs", "list", "-H", "-o", "name", full_parent])
    if probe.returncode == 0:
        return
    run_cmd(["zfs", "create", "-o", "mountpoint=none", full_parent])


def list_zvol_rows() -> list[dict]:
    output = run_cmd(["zfs", "list", "-H", "-t", "volume", "-o", "name,volsize,used,refer,origin"])
    rows: list[dict] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        rows.append({
            "name": parts[0],
            "volsize": parts[1],
            "used": parts[2],
            "refer": parts[3],
            "origin": parts[4] or "-",
        })
    return rows


def get_zvol_row_by_name(zvol_name: str) -> dict:
    for row in list_zvol_rows():
        if row["name"] == zvol_name:
            return row
    raise HTTPException(status_code=404, detail="ZVOL 不存在")


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


def list_zvol_snapshots(zvol_name: str, clone_names_by_origin: Optional[dict[str, list[str]]] = None) -> list[dict]:
    result = run_result(
        ["zfs", "list", "-H", "-t", "snapshot", "-o", "name,used,refer", "-r", zvol_name],
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
        snapshots.append({
            "name": parts[0],
            "short_name": parts[0].split("@", 1)[1],
            "used": parts[1],
            "refer": parts[2],
            "dependent_clones": clone_names_by_origin.get(parts[0], []) if clone_names_by_origin else [],
        })
    return snapshots


def find_dependent_clones(snapshot_name: str) -> list[str]:
    return [row["name"] for row in list_zvol_rows() if row["origin"] == snapshot_name]


# ---- 操作实现 ----

def create_zvol_impl(pool: str, name: str, parent_dataset: str, size: str,
                     volblocksize: str, compression: str, sync: str, sparse: bool) -> dict:
    full_parent = f"{pool}/{parent_dataset}"
    full_name = f"{full_parent}/{name}"
    ensure_parent_dataset(full_parent)

    args = ["zfs", "create", "-V", size.strip(), "-b", volblocksize.strip(),
            "-o", f"compression={compression.strip()}", "-o", f"sync={sync.strip()}", full_name]
    if sparse:
        args.insert(2, "-s")
    run_cmd(args, timeout=120)

    append_log("info", "zfs", "创建 ZVOL", {
        "object_type": "ZVOL", "object_name": full_name,
        "action": "create", "result": "success",
        "size": size, "sparse": sparse,
    })
    return {
        "message": "ZVOL 创建成功",
        "zvol_name": full_name,
        "device": zvol_device_path(full_name),
    }


def delete_zvol_impl(full_name: str) -> dict:
    run_cmd(["zfs", "destroy", "-r", full_name], timeout=120)
    append_log("info", "zfs", "删除 ZVOL", {
        "object_type": "ZVOL", "object_name": full_name,
        "action": "delete", "result": "success",
    })
    return {"message": "ZVOL 删除成功", "zvol_name": full_name}


def create_snapshot_impl(zvol_name: str, snapshot_short_name: str) -> dict:
    full_name = normalize_zvol_name(zvol_name)
    short_name = require_safe_name(snapshot_short_name, "快照名称")
    full_snapshot_name = f"{full_name}@{short_name}"
    run_cmd(["zfs", "snapshot", full_snapshot_name], timeout=120)
    append_log("info", "zfs", "创建快照", {
        "object_type": "Snapshot", "object_name": full_snapshot_name,
        "action": "create", "result": "success",
        "zvol_name": full_name,
    })
    return {"message": "ZVOL 快照创建成功", "zvol_name": full_name, "snapshot_name": full_snapshot_name}


def destroy_snapshot_impl(snapshot_name: str, promote_dependent_clones: bool = True) -> dict:
    full_snapshot_name = normalize_snapshot_name(snapshot_name, "快照名称")
    dependent_clones = find_dependent_clones(full_snapshot_name)
    promoted_clones: list[str] = []
    if dependent_clones and not promote_dependent_clones:
        return {"snapshot_name": full_snapshot_name, "promoted_clones": [], "skipped_clones": dependent_clones}
    for clone_name in dependent_clones:
        run_cmd(["zfs", "promote", clone_name], timeout=120)
        promoted_clones.append(clone_name)
    run_cmd(["zfs", "destroy", full_snapshot_name], timeout=120)
    append_log("info", "zfs", "删除快照", {
        "object_type": "Snapshot", "object_name": full_snapshot_name,
        "action": "delete", "result": "success",
        "promoted_clones": promoted_clones,
    })
    return {"snapshot_name": full_snapshot_name, "promoted_clones": promoted_clones, "skipped_clones": []}


def rollback_snapshot_impl(snapshot_name: str) -> dict:
    full_snapshot_name = normalize_snapshot_name(snapshot_name, "快照名称")
    run_cmd(["zfs", "rollback", "-r", full_snapshot_name], timeout=120)
    zvol_name = snapshot_dataset_name(full_snapshot_name)
    append_log("info", "zfs", "回滚快照", {
        "object_type": "Snapshot", "object_name": full_snapshot_name,
        "action": "rollback", "result": "success",
        "zvol_name": zvol_name,
    })
    return {"message": "ZVOL 快照回滚成功", "snapshot_name": full_snapshot_name, "zvol_name": zvol_name}


def clone_zvol_impl(source_snapshot: str, pool: str, parent_dataset: str, clone_name: str) -> dict:
    full_snapshot = normalize_snapshot_name(source_snapshot, "源快照")
    target_parent = f"{pool}/{parent_dataset}"
    target_name = f"{target_parent}/{clone_name}"
    ensure_parent_dataset(target_parent)
    run_cmd(["zfs", "clone", full_snapshot, target_name], timeout=120)
    append_log("info", "zfs", "克隆 ZVOL", {
        "object_type": "Clone", "object_name": target_name,
        "action": "clone", "result": "success",
        "source_snapshot": full_snapshot,
    })
    return {"message": "ZVOL 克隆创建成功", "source_snapshot": full_snapshot, "zvol_name": target_name, "device": zvol_device_path(target_name)}


def pipe_zfs_send_receive(base_snapshot: str, source_snapshot: str, target_dataset: str) -> None:
    preflight = run_result(["zfs", "send", "-nP", "-i", base_snapshot, source_snapshot], timeout=30)
    if preflight.returncode != 0:
        detail = (preflight.stderr or preflight.stdout or "").strip() or "增量同步预检查失败"
        raise HTTPException(status_code=500, detail=detail)

    # 清理目标上的快照
    target_snapshots = list_dataset_snapshots(target_dataset)
    if target_snapshots:
        for snap in target_snapshots:
            dependent_clones = find_dependent_clones(snap["name"])
            for clone_name in dependent_clones:
                run_cmd(["zfs", "promote", clone_name], timeout=120)
            run_result(["zfs", "destroy", "-d", snap["name"]], timeout=30)
        remaining = list_dataset_snapshots(target_dataset)
        if remaining:
            names = ", ".join(item["name"] for item in remaining)
            raise HTTPException(status_code=500, detail=f"目标 dataset {target_dataset} 仍有 {len(remaining)} 个快照无法清理：{names}")

    recv_proc = subprocess.Popen(
        ["zfs", "receive", "-F", target_dataset],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=False,
    )
    send_proc = subprocess.Popen(
        ["zfs", "send", "-i", base_snapshot, source_snapshot],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False,
    )
    if send_proc.stdout is None or recv_proc.stdin is None:
        send_proc.kill()
        recv_proc.kill()
        send_proc.wait()
        recv_proc.wait()
        raise HTTPException(status_code=500, detail="无法建立 zfs send/recv 管道")
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
    except Exception:
        send_proc.kill()
        recv_proc.kill()
        send_proc.wait()
        recv_proc.wait()
        raise
    finally:
        if send_proc.stdout is not None:
            send_proc.stdout.close()

    if send_code != 0:
        detail = send_stderr.decode("utf-8", errors="ignore").strip() or "zfs send 执行失败"
        raise HTTPException(status_code=500, detail=detail)
    if recv_code != 0:
        detail = recv_stderr.decode("utf-8", errors="ignore").strip() or "zfs receive 执行失败"
        raise HTTPException(status_code=500, detail=detail)

    # zfs receive -F 可能将目标 dataset 重命名为数据流中的源名称，需要改回原名
    check = run_result(["zfs", "list", "-H", "-o", "name", target_dataset], timeout=15)
    if check.returncode != 0:
        source_dataset = snapshot_dataset_name(source_snapshot)
        check2 = run_result(["zfs", "list", "-H", "-o", "name", source_dataset], timeout=15)
        if check2.returncode == 0:
            actual = check2.stdout.strip()
            if actual == source_dataset:
                run_cmd(["zfs", "rename", actual, target_dataset], timeout=120)


def reverse_sync_impl(snapshot_name: str, base_snapshot: Optional[str]) -> dict:
    full_snapshot_name = normalize_snapshot_name(snapshot_name, "快照名称")
    source_dataset = snapshot_dataset_name(full_snapshot_name)
    source_row = get_zvol_row_by_name(source_dataset)
    origin_snapshot = source_row.get("origin") or "-"
    if origin_snapshot == "-":
        raise HTTPException(status_code=409, detail="当前快照所属 ZVOL 不是 clone，无法执行增量反向同步")

    target_dataset = snapshot_dataset_name(origin_snapshot)
    get_zvol_row_by_name(target_dataset)
    base = normalize_snapshot_name(base_snapshot, "增量基线快照") if base_snapshot else origin_snapshot
    if snapshot_dataset_name(base) != target_dataset:
        raise HTTPException(status_code=400, detail="所选增量基线快照不属于 origin 数据集")

    pipe_zfs_send_receive(base, full_snapshot_name, target_dataset)

    append_log("info", "zfs", "反向同步", {
        "object_type": "Snapshot", "object_name": full_snapshot_name,
        "action": "reverse_sync", "result": "success",
        "source_dataset": source_dataset,
        "target_dataset": target_dataset,
        "base_snapshot": base,
    })
    return {"message": "增量反向同步成功", "base_snapshot": base, "source_snapshot": full_snapshot_name, "target_dataset": target_dataset}


def build_clone_sync_target(source_dataset: str, clone_row: dict, source_snapshot: str) -> dict:
    target_dataset = clone_row["name"]
    base_snapshot = clone_row["origin"]
    if base_snapshot == "-" or snapshot_dataset_name(base_snapshot) != source_dataset:
        raise HTTPException(status_code=409, detail=f"Clone {target_dataset} 的 origin 不属于当前源 ZVOL")
    pipe_zfs_send_receive(base_snapshot, source_snapshot, target_dataset)
    return {
        "clone_name": target_dataset,
        "base_snapshot": base_snapshot,
        "source_snapshot": source_snapshot,
        "target_dataset": target_dataset,
        "status": "success",
    }


def sync_origin_to_clones_impl(snapshot_name: str, requested_clone_names: set[str]) -> dict:
    source_snapshot = normalize_snapshot_name(snapshot_name, "快照名称")
    source_dataset = snapshot_dataset_name(source_snapshot)
    source_row = get_zvol_row_by_name(source_dataset)
    if source_row.get("origin") and source_row.get("origin") != "-":
        raise HTTPException(status_code=409, detail="只有 origin ZVOL 的快照才支持同步到 clone")

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
            failures.append({"clone_name": clone_row["name"], "status": "failed", "detail": exc.detail})

    append_log("info", "zfs", "同步到 clone", {
        "object_type": "Snapshot", "object_name": source_snapshot,
        "action": "sync_to_clones", "result": "success" if not failures else "partial",
        "source_dataset": source_dataset,
        "success_count": len(results),
        "failure_count": len(failures),
    })
    return {
        "message": "origin 增量同步已执行",
        "source_snapshot": source_snapshot,
        "results": results,
        "failures": failures,
    }
