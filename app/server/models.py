"""Pydantic 请求模型"""

from typing import Optional

from pydantic import BaseModel, Field


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
