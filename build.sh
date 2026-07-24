#!/bin/bash
# ============================================================
# ZVOL Manager — FPK 打包脚本
# 在飞牛 FNOS 系统上执行，需要已安装 fnpack 工具
# 用法: chmod +x build.sh && ./build.sh
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== ZVOL Manager FPK 打包 ==="
echo "项目目录: ${PROJECT_DIR}"

# 1. 校验项目结构
REQUIRED=(
    "manifest"
    "ICON.PNG"
    "ICON_256.PNG"
    "app/backend.py"
    "app/frontend/index.html"
    "app/requirements.txt"
    "app/ui/config"
    "cmd/main"
    "config/privilege"
    "config/resource"
    "wizard/install"
)

FAIL=0
for f in "${REQUIRED[@]}"; do
    if [ ! -e "${PROJECT_DIR}/${f}" ]; then
        echo "[ERROR] 缺少必要文件: ${f}"
        FAIL=1
    fi
done
if [ $FAIL -ne 0 ]; then
    echo "项目结构不完整，打包终止"
    exit 1
fi

# 2. 清理无关文件（避免打入 .DS_Store 等）
find "${PROJECT_DIR}" -name ".DS_Store" -delete 2>/dev/null || true

# 3. 检查 fnpack 是否可用
if ! command -v fnpack &> /dev/null; then
    echo ""
    echo "[ERROR] fnpack 命令未找到"
    echo "请先在飞牛系统中安装 fnpack 工具"
    echo "参考: curl -sSL https://fnos.example.com/install-fnpack.sh | bash"
    exit 1
fi

# 4. 打包
echo ""
echo "正在打包..."
cd "${PROJECT_DIR}"
fnpack build -d "${PROJECT_DIR}"

# 5. 输出结果 — fnpack 默认在当前目录生成 .fpk
FPK_FILE=$(ls "${PROJECT_DIR}"/*.fpk 2>/dev/null | head -1)
if [ -n "${FPK_FILE}" ]; then
    FPK_SIZE=$(du -h "${FPK_FILE}" | cut -f1)
    echo ""
    echo "=== 打包完成 ==="
    echo "产物: ${FPK_FILE}"
    echo "大小: ${FPK_SIZE}"
    echo ""
    echo "安装方式（飞牛桌面端）:"
    echo "  系统设置 → 应用中心 → 手动安装 → 选择 ${FPK_FILE}"
    echo ""
    echo "或命令行安装:"
    echo "  fnpack install ${FPK_FILE}"
else
    echo "[ERROR] 未生成 .fpk 文件，请检查 fnpack build 输出"
    exit 1
fi
