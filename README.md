# ZFS ZVOL Manager

飞牛 fnOS 的 ZFS ZVOL 管理应用（FPK 原生应用，非 Docker）。

支持 ZFS 存储池上的 ZVOL（ZFS Volume）全生命周期管理：

- 创建 / 删除 ZVOL
- 挂载（ext4 格式化 + mount）/ 卸载
- 快照管理（创建、列表、回滚、删除）
- 从快照克隆

---

## 系统要求

- 飞牛 fnOS >= 0.8.0
- 系统已安装 ZFS（`zfs`、`zpool` 命令可用）
- 已有至少一个 ZFS 存储池

---

## 项目结构

```
zvol-manager/
├── app/
│   ├── backend.py              # FastAPI 后端
│   ├── requirements.txt        # Python 依赖
│   ├── frontend/
│   │   └── index.html          # 前端 SPA（Vue.js CDN）
│   └── ui/
│       ├── images/
│       │   ├── icon-64.png
│       │   └── icon-256.png
│       └── config              # 桌面入口配置
├── cmd/                        # 生命周期脚本
│   ├── main                    # start / stop / status
│   ├── install_init
│   ├── install_callback        # pip install
│   ├── uninstall_init
│   ├── uninstall_callback      # 清理 PID 和日志
│   ├── upgrade_init
│   ├── upgrade_callback        # pip install
│   ├── config_init
│   └── config_callback
├── config/
│   ├── privilege               # run-as: root（ZFS 需要 root 权限）
│   └── resource
├── wizard/
│   └── install                 # 安装向导：设置 Web 端口
├── manifest                    # 应用元信息
├── ICON.PNG                    # 64x64 图标
├── ICON_256.PNG                # 256x256 图标
├── generate_icons.py           # 图标生成脚本
└── README.md
```

---

## 打包

### 1. 生成图标

```bash
python3 generate_icons.py
```

这会生成 `ICON.PNG`、`ICON_256.PNG` 以及 `app/ui/images/icon-64.png`、`app/ui/images/icon-256.png`。

### 2. 安装 fnpack CLI

参见飞牛官方文档：https://developer.fnnas.com/docs/guide/

### 3. 构建 .fpk

```bash
cd zvol-manager
fnpack build
```

构建成功后会在当前目录生成 `zvol-manager_1.0.0_all.fpk`。

---

## 安装

### 方式一：应用中心手动安装

1. 打开飞牛 fnOS 桌面，进入「应用中心」
2. 点击「手动安装」
3. 选择 `zvol-manager_1.0.0_all.fpk`
4. 在安装向导中设置 Web 管理端口（默认 `18888`）
5. 等待安装完成

### 方式二：命令行安装

```bash
appcenter-cli install-fpk zvol-manager_1.0.0_all.fpk
```

---

## 使用

安装完成后，在飞牛桌面点击「ZVOL Manager」图标，或直接访问：

```
http://<飞牛IP>:18888
```

### Web 界面功能

| 页面 | 功能 |
|------|------|
| ZVOL 列表 | 查看所有 ZVOL，含大小、已用、Block Size、压缩比、挂载状态等 |
| ZVOL 操作 | 挂载/卸载、快照管理、从快照克隆、删除 |
| 创建 ZVOL | 选择存储池、设置名称/大小/volblocksize/压缩算法/稀疏模式 |
| 快照管理 | 创建快照、回滚到指定快照、删除快照 |
| 克隆 | 从快照克隆 ZVOL 到目标存储池 |

### 危险操作确认

以下操作有前端二次确认弹窗：

- 删除 ZVOL（数据不可恢复）
- 删除快照
- 回滚到快照（当前数据丢失）

---

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/pools` | 列出所有 ZFS 存储池 |
| GET | `/api/zvols` | 列出所有 ZVOL |
| POST | `/api/zvols` | 创建 ZVOL |
| DELETE | `/api/zvols/{name}` | 删除 ZVOL |
| POST | `/api/zvols/{name}/mount` | 格式化 ext4 并挂载 |
| POST | `/api/zvols/{name}/unmount` | 卸载 ZVOL |
| POST | `/api/zvols/{name}/snapshot` | 创建快照 |
| GET | `/api/zvols/{name}/snapshots` | 列出快照 |
| POST | `/api/zvols/{name}/rollback` | 回滚到指定快照 |
| DELETE | `/api/zvols/{name}/snapshots/{snap}` | 删除快照 |
| POST | `/api/zvols/{name}/clone` | 从快照克隆 ZVOL |

---

## 注意事项

1. **root 权限**：应用以 root 身份运行，因为 ZFS 命令需要 root 权限（`config/privilege` 声明 `run-as: root`）
2. **ZFS 环境**：确保系统已正确安装和配置 ZFS，且至少有一个存储池
3. **端口冲突**：安装向导可自定义 Web 端口，避免与已有服务冲突
4. **数据安全**：删除和回滚操作不可逆，请谨慎操作
5. **挂载依赖**：挂载功能依赖 `mkfs.ext4` 和标准 `mount` 命令
