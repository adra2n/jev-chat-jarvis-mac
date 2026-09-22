#!/bin/zsh
# 启动 jev-jarvis 悬浮窗（不装 LaunchAgent，按需手动启动）
cd "$(dirname "$0")"
export USE_TF=0
# 使用 jev-jarvis 虚拟环境
VENV_DIR="$HOME/Desktop/project/.venv-jev-jarvis"
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
else
    print "未找到虚拟环境: $VENV_DIR"
    print "请先创建虚拟环境: python3 -m venv $VENV_DIR"
    exit 1
fi
# uv installs to ~/.local/bin; a Finder-launched .command does not inherit a login shell
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
# same user-level env the .app launcher uses (API keys live outside the repo)
[ -f "$HOME/.config/jev-jarvis/env" ] && source "$HOME/.config/jev-jarvis/env"
# 启用离线模式（模型已缓存）
export HF_HUB_OFFLINE=1

exec python3 src/hud.py
