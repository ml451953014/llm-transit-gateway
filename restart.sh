#!/bin/bash
# 停止旧进程并重启中转服务（委托给 start.sh 自动探测 Python 3.11 并启动）
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/start.sh"
