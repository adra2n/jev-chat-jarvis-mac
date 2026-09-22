#!/bin/zsh
# 启动 jev-jarvis 悬浮窗（不装 LaunchAgent，按需手动启动）
cd "$(dirname "$0")"
export USE_TF=0
# uv installs to ~/.local/bin; a Finder-launched .command does not inherit a login shell
# 必须在激活 venv **之前**：放后面会把 /usr/local/bin 插到 venv 前面，
# exec python3 就会挑中 Homebrew 3.14（无 objc，且违反本项目必须 3.11 的约束）。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
# same user-level env the .app launcher uses (API keys live outside the repo)
[ -f "$HOME/.config/jev-jarvis/env" ] && source "$HOME/.config/jev-jarvis/env"
# 启用离线模式（模型已缓存）
export HF_HUB_OFFLINE=1
# 使用 jev-jarvis 虚拟环境——最后激活，让它排在 PATH 最前
VENV_DIR="$HOME/Desktop/project/.venv-jev-jarvis"
if [ ! -d "$VENV_DIR/bin" ]; then
    print "未找到虚拟环境: $VENV_DIR"
    print "请先创建虚拟环境: uv venv --python 3.11 $VENV_DIR"
    exit 1
fi
source "$VENV_DIR/bin/activate"
# 绝对路径执行：不依赖 PATH 排序，激活没生效也会在这里暴露
exec "$VENV_DIR/bin/python3" src/hud.py
