#!/bin/bash
# 小红书自动回复工作流 - 启动脚本
# 清除 TRAE 环境变量，使用系统 Python 运行

# 清除可能干扰的环境变量
unset PYTHONHOME
unset PYTHONPATH
unset PYTHONIOENCODING
unset PYTHONUTF8

# 使用 Homebrew Python 3.12（稳定可用）
PYTHON=/opt/homebrew/bin/python3.12

# 检查依赖是否安装
$PYTHON -c "import loguru" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "正在安装依赖..."
    $PYTHON -m pip install loguru httpx pydantic pydantic-settings playwright --break-system-packages
fi

# 启动工作流
cd "$(dirname "$0")"
exec $PYTHON main.py
