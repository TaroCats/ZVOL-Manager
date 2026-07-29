# ZVOL Manager

面向 fnOS / Linux 平台的 ZFS ZVOL 与 iSCSI Target Web 管理工具。通过图形界面管理 ZFS 块卷、快照、克隆、增量同步，以及 LIO iSCSI Target 的完整生命周期。

## 功能概览

### ZFS ZVOL 管理

- **创建 ZVOL** — 支持厚置备和稀疏卷，可自定义 volblocksize、压缩算法、sync 模式
- **删除 ZVOL** — 自动 promote 依赖的 clone，支持强制删除
- **ZVOL 列表** — 展示容量、已用空间、压缩率、volblocksize、origin 链路等完整属性
- **父数据集自动创建** — 创建 ZVOL 时自动创建父 dataset（mountpoint=none）

### 快照与克隆

- **快照创建 / 删除** — 删除时自动 promote 依赖的 clone，避免 busy 错误
- **快照回滚** — 使用 `zfs rollback -r` 回滚到指定快照
- **克隆创建** — 从快照创建可写克隆卷，展示完整 origin 来源链
- **增量反向同步** — 将 clone 的变更反向同步回 origin（`zfs send -i | zfs recv -F`）
- **Origin 推送到 Clone** — 将 origin 快照增量同步到多个 clone
- **Clone Push** — 将 clone 改动回合到 origin（自动 pull 合并、打快照、send/recv、重建 clone）
- **Clone Pull** — 将 origin 最新状态同步到 clone，保留 clone 自身改动

### 定时快照

- 为指定 ZVOL 创建定时快照任务
- 可配置快照前缀、执行周期（分钟）、保留数量
- 自动清理超出保留数量的旧快照（跳过有 clone 依赖的快照）
- 任务状态持久化，支持启用/停用

### iSCSI Target 管理

- **Backstore** — 将 ZVOL 设备注册为 LIO IBLOCK 后端，支持创建/删除/列表
- **iSCSI Target** — 创建/删除 iSCSI Target，支持多 TPG
- **LUN 挂载** — 将 Backstore 挂载到 Target 的指定 TPG
- **Portal 管理** — 添加/删除监听 IP 和端口
- **ACL 管理** — 添加/删除 Initiator ACL，控制接入权限
- **CHAP 认证** — Target 级和 ACL 级的 CHAP 认证，含 Mutual CHAP 支持
- **Target 设置** — 认证开关、generate_node_acls 等属性配置
- **实时状态** — 通过读取 `/sys/kernel/config/target` 获取实时状态

### 系统功能

- **运行环境检测** — 自动检测 Linux 平台、ConfigFS 可用性、命令可用性
- **操作日志** — 所有命令执行记录持久化到日志文件
- **推荐配置模板** — 内置存储池、ZVOL、LIO 的推荐配置方案
- **输入校验** — 所有用户输入经过正则校验，防止路径穿越

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端框架 | FastAPI |
| ASGI 服务器 | uvicorn |
| 数据验证 | Pydantic v2 |
| 前端 | Vue 3（单页应用） |
| 底层命令 | `zfs`, `zpool`, `targetcli` |
| 内核接口 | Linux ConfigFS |
| 构建工具 | fnpack |

## 架构

```
zvol-manager/
├── app/
│   ├── backend.py              # FastAPI 主入口，API 路由定义
│   ├── requirements.txt        # Python 依赖
│   ├── server/
│   │   ├── models.py           # 请求/响应模型
│   │   ├── utils.py            # 通用工具函数
│   │   ├── zfs_ops.py          # ZFS/ZVOL 操作实现
│   │   ├── iscsi_ops.py        # iSCSI/LIO 操作实现
│   │   └── scheduler.py        # 定时快照调度器
│   ├── frontend/
│   │   └── index.html          # Vue 3 单页前端
│   └── ui/
│       └── config              # fnOS 桌面入口配置
├── cmd/                        # 生命周期脚本
├── config/                     # 权限与资源配置
├── manifest                    # 应用元信息
├── build.sh                    # 构建脚本
└── wizard/install              # 安装向导
```

**数据流**: 前端 (Vue 3) → HTTP REST API → FastAPI 后端 → 子进程调用 `zfs`/`targetcli` → Linux 内核 (ZFS / LIO ConfigFS)

## API 端点

### 系统

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/version` | 应用版本号 |
| GET | `/api/profile` | 推荐配置模板 |
| GET | `/api/health` | 运行环境检测 |

### 存储池

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/pools` | 列出 ZFS 存储池 |

### ZVOL

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/zvols` | 列出所有 ZVOL（含属性、快照、导出状态、克隆关系） |
| POST | `/api/zvols` | 创建 ZVOL |
| DELETE | `/api/zvols/{name}` | 删除 ZVOL |
| POST | `/api/zvols/clones` | 从快照创建克隆 |
| POST | `/api/zvols/clones/{name}/push` | Clone Push 回合到 origin |
| POST | `/api/zvols/clones/{name}/pull` | Clone Pull 从 origin 同步 |

### 快照

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/zvols/{name}/snapshots` | 创建快照 |
| DELETE | `/api/zvol-snapshots/{name}` | 删除快照 |
| POST | `/api/zvol-snapshots/{name}/rollback` | 回滚到快照 |
| POST | `/api/zvol-snapshots/{name}/reverse-sync` | 增量反向同步 |
| POST | `/api/zvol-snapshots/{name}/sync-to-clones` | 推送到 clone |

### 定时快照

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/snapshot-jobs` | 列出定时任务 |
| POST | `/api/snapshot-jobs` | 创建定时任务 |
| PUT | `/api/snapshot-jobs/{id}` | 更新定时任务 |
| DELETE | `/api/snapshot-jobs/{id}` | 删除定时任务 |

### Backstore

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/backstores` | 列出 Backstore |
| POST | `/api/backstores` | 创建 Backstore |
| DELETE | `/api/backstores/{name}` | 删除 Backstore |

### iSCSI Target

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/iscsi/targets` | 列出 Target |
| POST | `/api/iscsi/targets` | 创建 Target |
| DELETE | `/api/iscsi/targets/{iqn}` | 删除 Target |
| POST | `/api/iscsi/targets/{iqn}/luns` | 挂载 LUN |
| GET | `/api/iscsi/targets/{iqn}/settings` | 读取设置 |
| PUT | `/api/iscsi/targets/{iqn}/settings` | 更新设置 |
| POST | `/api/iscsi/targets/{iqn}/portals` | 添加 Portal |
| DELETE | `/api/iscsi/targets/{iqn}/portals` | 删除 Portal |
| POST | `/api/iscsi/targets/{iqn}/acls` | 添加 ACL |
| DELETE | `/api/iscsi/targets/{iqn}/acls` | 删除 ACL |
| PUT | `/api/iscsi/targets/{iqn}/acls/{initiator}/chap` | 更新 CHAP |

## 构建与部署

```bash
# 构建 fnOS 应用包
./build.sh

# 手动启动（开发调试）
cd app
python3 -m uvicorn backend:app --host 0.0.0.0 --port 8765
```

## 推荐配置

内置推荐配置模板，适用于典型 iSCSI 存储场景：

- **存储池**: ashift=12, compression=lz4, atime=off, xattr=sa, acltype=posix
- **ZVOL**: volblocksize=16K, compression=lz4, sync=standard
- **LIO**: IBLOCK 后端, block_size=512, queue_depth=128, is_nonrot=1
