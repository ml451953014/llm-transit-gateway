#!/bin/bash
# ==============================================================================
# start.sh: 自动探测 Conda 环境中的 Python 3.11 并启动中转服务 (proxy.py)
# ==============================================================================
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PROXY_PORT=4000
LITELLM_PORT=4001

# 运维脚本与应用读取同一份端口配置；解析失败时保留默认值。
CONFIG_PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
if [[ -n "$CONFIG_PY" && -f "$DIR/providers_config.json" ]]; then
    PORTS="$($CONFIG_PY - "$DIR/providers_config.json" 2>/dev/null <<'PY' || true
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    server = (json.load(f).get("server") or {})
print(server.get("proxy_port", 4000), server.get("litellm_port", 4001))
PY
)"
    if [[ "$PORTS" =~ ^[0-9]+\ [0-9]+$ ]]; then
        PROXY_PORT="${PORTS%% *}"
        LITELLM_PORT="${PORTS##* }"
    fi
fi

echo "=================================================="
echo " [1/3] 正在探测 Python 3.11 环境路径..."
echo "=================================================="

# proxy.py 靠 sys.executable 的同级目录去找 litellm 可执行文件，所以候选 Python
# 必须同时满足「是 3.11」和「同级目录有 litellm」——只看版本号会挑中当前激活但没装
# litellm 的环境（例如 ai-supervisor-backend），启动到一半才崩。
is_usable_python() {
    local py="$1"
    [[ -x "$py" ]] || return 1
    "$py" --version 2>&1 | grep -q "3\.11" || return 1
    [[ -x "$(dirname "$py")/litellm" ]] || return 1
    return 0
}

find_python_311() {
    # 1. 优先从当前正在运行的相关服务进程 (4000/4001 端口或 proxy.py/litellm) 探测 Python 路径
    for pid in $(lsof -ti :$PROXY_PORT :$LITELLM_PORT 2>/dev/null) $(pgrep -f "proxy.py|litellm" 2>/dev/null); do
        cmd=$(ps -p "$pid" -o command= 2>/dev/null | awk '{print $1}')
        if is_usable_python "$cmd"; then
            echo "$cmd"
            return 0
        fi
    done

    # 2. 尝试通过 conda 命令查找环境列表（支持多种常见 conda 安装位置）
    local conda_bins=(
        "conda"
        "/opt/homebrew/anaconda3/bin/conda"
        "/opt/homebrew/miniconda3/bin/conda"
        "/opt/anaconda3/bin/conda"
        "/opt/miniconda3/bin/conda"
        "$HOME/anaconda3/bin/conda"
        "$HOME/miniconda3/bin/conda"
        "$HOME/opt/anaconda3/bin/conda"
        "$HOME/opt/miniconda3/bin/conda"
    )

    for cb in "${conda_bins[@]}"; do
        if command -v "$cb" >/dev/null 2>&1 || [[ -x "$cb" ]]; then
            # 先挑名字叫 py311 的环境，避免被其它同为 3.11 的环境抢先
            for env_path in $("$cb" info --envs 2>/dev/null | awk '{print $NF}' | grep '^/' | grep '/py311$') \
                            $("$cb" info --envs 2>/dev/null | awk '{print $NF}' | grep '^/'); do
                if is_usable_python "$env_path/bin/python"; then
                    echo "$env_path/bin/python"
                    return 0
                fi
            done
        fi
    done

    # 3. 常见 Conda/Anaconda/Miniconda 的 py311 预设路径兜底
    local common_paths=(
        "/opt/homebrew/anaconda3/envs/py311/bin/python"
        "/opt/homebrew/miniconda3/envs/py311/bin/python"
        "/opt/anaconda3/envs/py311/bin/python"
        "/opt/miniconda3/envs/py311/bin/python"
        "$HOME/anaconda3/envs/py311/bin/python"
        "$HOME/miniconda3/envs/py311/bin/python"
        "$HOME/.conda/envs/py311/bin/python"
        "$HOME/opt/anaconda3/envs/py311/bin/python"
        "$HOME/opt/miniconda3/envs/py311/bin/python"
    )
    for p in "${common_paths[@]}"; do
        if is_usable_python "$p"; then
            echo "$p"
            return 0
        fi
    done

    # 4. 当前 PATH 中的 python3 / python
    for p in $(which python3 python 2>/dev/null); do
        if is_usable_python "$p"; then
            echo "$p"
            return 0
        fi
    done

    return 1
}

# 允许外部覆盖：PYTHON_BIN=/path/to/python ./start.sh
if [[ -n "${PYTHON_BIN:-}" ]]; then
    echo " -> 使用外部指定的 PYTHON_BIN"
else
    PYTHON_BIN="$(find_python_311 || true)"
fi

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
    echo "[ERROR] 未能定位到「同时装有 litellm」的 Python 3.11 环境！"
    echo ""
    echo "扫描结果："
    for cb in "conda" "/opt/homebrew/anaconda3/bin/conda" "/opt/miniconda3/bin/conda"; do
        if command -v "$cb" >/dev/null 2>&1 || [[ -x "$cb" ]]; then
            for env_path in $("$cb" info --envs 2>/dev/null | awk '{print $NF}' | grep '^/'); do
                py="$env_path/bin/python"
                [[ -x "$py" ]] || continue
                ver="$("$py" --version 2>&1)"
                if [[ -x "$env_path/bin/litellm" ]]; then lt="litellm 已装"; else lt="缺 litellm"; fi
                echo "  - $ver / $lt  ($env_path)"
            done
            break
        fi
    done
    echo ""
    echo "修复：在目标 3.11 环境里安装依赖"
    echo "  conda run -n py311 pip install -r requirements.txt"
    echo "或直接指定：PYTHON_BIN=/path/to/python $0"
    exit 1
fi

PY_VER="$("$PYTHON_BIN" --version 2>&1)"
echo " -> 成功找到 Python: $PYTHON_BIN"
echo " -> Python 版本: $PY_VER"

# litellm 必须在 Python 同级目录（proxy.py 靠 sys.executable 定位它）。
# 外部指定 PYTHON_BIN 时也要拦，否则同样会在 start_litellm() 里崩。
LITELLM_CHECK="$(dirname "$PYTHON_BIN")/litellm"
if [[ ! -x "$LITELLM_CHECK" ]]; then
    echo "[ERROR] $PYTHON_BIN 同级目录没有 litellm：$LITELLM_CHECK"
    echo "        proxy.py 启动子进程时会直接 FileNotFoundError，这里提前拦住。"
    echo "        修复：$(dirname "$PYTHON_BIN")/pip install -r requirements.txt"
    exit 1
fi
echo " -> litellm: $LITELLM_CHECK"

# 只探测不启动：DRY_RUN=1 ./start.sh
if [[ -n "${DRY_RUN:-}" ]]; then
    echo ""
    echo "[DRY_RUN] 探测通过，未启动服务。"
    exit 0
fi

echo ""
echo "=================================================="
echo " [2/3] 清理旧端口监听 (:${PROXY_PORT} / :${LITELLM_PORT})..."
echo "=================================================="
lsof -ti "tcp:${PROXY_PORT}" -s TCP:LISTEN | xargs kill -9 2>/dev/null || true
lsof -ti "tcp:${LITELLM_PORT}" -s TCP:LISTEN | xargs kill -9 2>/dev/null || true
sleep 0.5

echo ""
echo "=================================================="
echo " [3/3] 启动 proxy.py 网关服务（按 Ctrl+C 退出）..."
echo "=================================================="
exec "$PYTHON_BIN" proxy.py
