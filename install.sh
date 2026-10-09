#!/bin/bash
# 在 conda py311 环境中安装依赖
set -e
conda run -n py311 pip install -r "$(dirname "$0")/requirements.txt"
echo "安装完成，运行方式："
echo "  export GEMINI_API_KEY=your_key"
echo "  conda run -n py311 python 中转服务/proxy.py"
