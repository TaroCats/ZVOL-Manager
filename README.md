# ZVOL Manager

一个按 FNOS 原生 `fpk` 目录规范组织的 ZFS ZVOL + iSCSI 管理应用。

这次实现直接以你提供的帖子内容为准，不再依赖 fnOS 官方 iSCSI 应用做读写配置。应用内部直接调用 `zfs` 与 `targetcli`，完成下面这条链路：

1. 创建 ZVOL
2. 注册为 `LIO /backstores/block` 的 `IBLOCK`
3. 创建 iSCSI target
4. 单独为 target 挂载 LUN
5. 配置 Portal
6. 配置 ACL
7. 配置 CHAP

## 设计基线

默认按帖子里的推荐参数来生成配置提示：

- ZFS：`ashift=12`、`compression=lz4`、`atime=off`
- ZVOL：`volblocksize=16K`、`sync=standard`
- LIO：`IBLOCK`
- 逻辑扇区：保持 `512B`
- `queue_depth=128`
- `is_nonrot=1`
- `emulate_write_cache=0`

## 当前功能

- 读取 ZFS 存储池并用于创建表单下拉选择
- 查看 ZVOL 列表与核心参数
- 创建 ZVOL
- 创建 / 删除 `IBLOCK backstore`
- 创建 / 删除 iSCSI target
- 单独创建 LUN 并挂载已有 backstore
- 管理 Portal
- 管理 ACL
- 给 Target 或 ACL 写入 CHAP
- 在界面内给出 MCS 使用说明
- 输出环境诊断信息，判断是否运行在可用的 fnOS / Linux 环境

## 目录结构

```text
zvol-manager/
├── app/
│   ├── backend.py
│   ├── requirements.txt
│   ├── frontend/
│   │   └── index.html
│   └── ui/
│       ├── config
│       └── images/
├── cmd/
│   ├── main
│   ├── install_init
│   ├── install_callback
│   ├── uninstall_init
│   ├── uninstall_callback
│   ├── upgrade_init
│   ├── upgrade_callback
│   ├── config_init
│   └── config_callback
├── config/
│   ├── privilege
│   └── resource
├── wizard/
│   └── install
├── manifest
├── ICON.PNG
└── ICON_256.PNG
```

## 运行要求

- fnOS 1.1.8+
- root 权限
- 已安装并启用 ZFS
- 主机存在 `targetcli`
- 主机已启用 LIO / configfs

## 打包

如本机已安装 `fnpack`，可在项目根目录执行：

```bash
fnpack build
```

## 当前限制

当前开发环境是 macOS，因此这里只做了：

- 目录规范生成
- 后端与前端实现
- JSON / Shell / Python 语法级校验

真正的 ZFS、LIO、`targetcli` 运行验证需要在 fnOS 机器上安装后测试。

## 说明

现在这版已经把帖子里主链路和常用 target 侧配置都补到了应用里：

- ZVOL
- IBLOCK backstore
- 分离的 iSCSI target / LUN 创建流程
- Portal
- ACL
- CHAP

关于 MCS：

- LIO / targetcli 本身没有一个单独叫“开启 MCS”的独立开关
- 实际上通常是通过多 portal / 多路径，再结合 initiator 侧的多连接或 multipath 来实现
- 所以应用里把它做成了说明与配套配置入口，而不是伪造一个无效按钮
