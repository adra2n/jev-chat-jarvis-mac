# jev-chat-jarvis（macOS）

微信弹出一条消息 → 悬浮窗立刻告诉你**这句话的真实意图**、**风险几级**、**该留意什么**。

**纯只读、零封号风险**——不注入、不 hook、不解密数据库，只是「看屏幕 + 本地模型判断」。

![演示：微信群消息进来 → 悬浮窗给出意图与风险分级](docs/demo.gif)

> 注：`docs/demo.gif` 是移除生成层**之前**录的，画面里的候选回复区和「填入」按钮在当前版本已经没有了。

## 交流反馈

用着有问题、想提需求、想一起改，扫码进群（二维码 7 天失效，过期了在 issue 说一声）：

<img src="docs/wechat-group.png" width="200" alt="扫码加入微信交流群">

## 它能做什么

- **意图 + 风险**：8 类意图零样本 **86.4%**（22 条回归口径），风险 0–9 分级 + 行动建议，本地模型一次前向出全分布
- **纯本地、零外传**：判断走本地 Laya（模型已在缓存，`HF_HUB_OFFLINE=1` 全程离线），**不向任何外部服务发送数据**；面板上没有候选回复、没有填入
- **快**：消息一出现预判就起跑，停稳后直接复用预判结论上屏——本机实测预判 0.5–1.3 s、读屏+OCR ~1.5–2 s、停稳到上屏 ~1.6–2.6 s（以日志实测为准）
- **YOLO 检测框**（可选，`JEV_BOXES=1` 启动即开、菜单栏可切）：OCR 命中的消息实时框在微信窗口上，对方/我分色 + 置信度
- **桌宠**（默认开，`JEV_PET=0` 关）：HUD 旁边矢量画的圆脸小人——意图给表情、风险给描边色（绿/琥珀/红），待机眨眼呼吸。**信息面板默认隐藏，点桌宠才出现**，再点收起、不自动收；菜单栏「桌宠」关掉后信息面板自动改常驻

## 用法

**只想用**：[Releases](https://github.com/jev-chat/jev-chat-jarvis-mac/releases) 下载 `.app`，解压拖进「应用程序」，**第一次右键 → 打开**（没做公证，双击会被 Gatekeeper 拦）。

首次启动按提示授予「屏幕录制」权限（系统设置 › 隐私与安全性 › 录屏与系统录音，给 **jev-jarvis** 打开），**退出重开**生效。v0.3.1 及更早的旧版本还需把 **python3.12** 那条一并打开。

**从源码跑**（微信在运行、终端已授予屏幕录制）：`./start.command`（激活 `.venv-jev-jarvis` → 置 `HF_HUB_OFFLINE=1` → 起 `src/hud.py`）。分层自测（先 `source ~/Desktop/project/.venv-jev-jarvis/bin/activate`）：

```bash
python3 -B -m unittest discover -s tests -v      # 消息几何/归属/判断触发门（离线，不读屏）
python3 probe/perception_regression.py           # 感知层回归（合成 OCR，离线）
python3 src/perception.py                        # 感知层：识别到的消息 + 耗时
python3 src/judge.py "这个需求你今天跟一下"        # 单条消息出判断
python3 src/judge_zh_test.py                     # 22 条中文意图回归
```

## 配置

**不需要填任何 key**：判断走本地 Laya（模型已在缓存里），生成层已移除，所以**默认零数据外发**。全部配置在一个 env 文件（**不提供第二种格式**）：

```bash
mkdir -p ~/.config/jev-jarvis
cat > ~/.config/jev-jarvis/env <<'ENV'
# 判断层（可选）：TypeSafe Jev。留空 = 本地 Laya，也是「零外传」的默认形态
export TYPESAFE_API_KEY=""

# 可选：启动时就打开 YOLO 检测框（菜单栏也能切）
# export JEV_BOXES=1

# 可选：关掉桌宠（默认开）
# export JEV_PET=0
ENV
chmod 600 ~/.config/jev-jarvis/env
```

- `OPENAI_*` / `ANTHROPIC_*` / `JEV_TONES` / `JEV_GENERATION` 已**没有任何代码读取**，填了无效（见 `.env.example`）
- 自查判断层（不打印任何凭据）：`python3 src/judge.py "这个需求你今天跟一下"`

## 已知限制

- 收发方向靠文字位置判断：横跨左右或居中、无法确认方向的文本标「方向未确认」，**不触发判断**；只有明确识别为「对方」的消息才触发判断，只有自己消息时面板显示「等待可确认的对方消息…」
- 图片/表情包读不出内容；引用回复当普通文本；公众号卡片可能被当消息解读
- **布局常量按固定窗口尺寸标定**（1440×814pt，`src/perception.py` 顶部三个常量，用户确认窗口不会改）：微信改版**或改动窗口大小**都要重新校准，否则会把左侧气泡裁得只剩一个字、或把输入框里的草稿当成消息
- 多窗口时优先识别主窗口「微信 / WeChat」
- 启动后第一条慢是正常现象（本地模型预热实测 ~26s，之后预判 0.5–1.3s）；不对劲先看日志（分阶段耗时、**不含消息正文**，可放心贴 issue）：`tail -40 ~/Library/Logs/jev-jarvis.log`

## 下一步（按优先级）

1. **攒标注数据**：把误判的（尤其「催进度 vs 问进度」）记下来，微调冲 95%+
2. **区分聊天消息和分享的文章卡片**：保守过滤，风险是误杀正常消息

## 开发者

- **贡献前必读**：[CONTRIBUTING.md](CONTRIBUTING.md)——动代码前先在 issue 认领（评论 + assignee），分层自测改哪层跑哪层
- 打包 `./packaging/build_app.sh`；发版 `./packaging/release.sh --publish`（干净 worktree 构建 + 解压回验 + gh release）。版本号只有 `pyproject.toml` 一处；有开发者证书可加 `--sign "Developer ID Application: ..."`
- 架构一句话：进程内抓微信窗口 → Vision OCR（只扫聊天区，边界按固定窗口尺寸标定）→ 本地 Laya 出意图/风险 → 悬浮窗 NSPanel。抓窗口不抓屏：微信被挡住也能抓，悬浮窗不污染 OCR

## 许可与免责

MIT（见 `LICENSE`）。只读**你自己屏幕上、你自己账号的**聊天内容，不注入、不 hook、不解密数据库、不自动发送任何消息。请在自己设备上自用；装到别人机器上读别人的聊天记录是另一回事，本项目不为那种用法背书。微信改版可能导致布局识别失效，请遵守微信软件许可协议。
