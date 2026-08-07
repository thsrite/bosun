# Windows 版规划（二期）

> 状态：规划中，未实施。一期（v0.2.0）已发 macOS / Linux 二进制，本文规划 Windows exe 的移植路线。

## 目标与非目标

**目标**
- 发布 `bosun-<tag>-windows-x86_64.zip`（PyInstaller onedir，含 `bosun.exe`），双击/命令行启动后浏览器访问 `http://127.0.0.1:8770`，与 Linux 形态对齐
- 任务执行全链路可用：创建任务 → 引擎 CLI 在 ConPTY 里跑 → 浏览器实时终端 → 插手/接管
- GitHub Actions 增加 windows 构建 job，与现有 release 流水线合并

**非目标（首版不做）**
- 托盘常驻程序（对应 macOS 菜单栏 app）——首版就是控制台 exe
- Windows 服务化 / 开机自启
- 二进制在线自更新（见「风险与延后项」，Windows 文件锁语义不同，延到 M3）

## 现状盘点：POSIX 依赖清单（已逐处核实）

| # | 位置 | 依赖 | Windows 对策 |
|---|---|---|---|
| 1 | `pty_session.py` | `ptyprocess.PtyProcess`，实际只用 8 个方法：`spawn / read / write / setwinsize / isalive / wait / exitstatus / terminate` | `pywinpty`（ConPTY）提供同型 `PtyProcess` 接口，jupyter/terminado 即用此双轨结构，先例成熟。新建 `pty_compat.py` 按平台选实现，会话层不动 |
| 2 | `engine_models.py` | 直接用 `pty` + `fcntl` + `termios`（TIOCSWINSZ）拉模型列表 | 改走 `pty_compat` 同一适配层（或 Windows 下退化为管道 subprocess） |
| 3 | `scheduler.py` | `fcntl.flock` 跨进程调度锁 | 小 shim：Windows 用 `msvcrt.locking`（或引入 `portalocker`，倾向前者零依赖） |
| 4 | `pty_session.py` script 录制 | `script` 命令（bsd/util-linux 探测） | 已优雅降级：探测不到自动禁用录制，无需改 |
| 5 | `bosun_skills/bosun-report` | `bash scripts/report.sh` | 引擎 CLI（claude 等）在 Windows 常带 Git Bash，但不可假定：报告脚本补 `report.ps1` 或改 python 实现，SKILL.md 按平台给命令 |
| 6 | `config.py` 引擎探测 | 候选路径全是 POSIX（`~/.local/bin`、`/opt/homebrew`） | `shutil.which` 在 Windows 天然找 `claude.cmd`；补 `%APPDATA%\npm` 候选。**注意**：npm 装的 CLI 是 `.cmd` shim，ConPTY spawn 需经 `cmd /c` 包一层，在 `pty_compat` 内统一处理 |
| 7 | 自更新 `self_update.py` | 整包 rename 替换 | Windows 禁止改名含运行中 exe 的目录 → 首版禁用（blocker 提示手动下载），M3 用「退出后 .bat 换包重启」方案 |
| 8 | launchd / 菜单栏 | macOS 专属 | 不移植，首版控制台 exe |

其余已核实无碍：后端无 `os.fork`/信号处理；uvicorn + websockets 在 Windows 正常；前端 xterm.js 对 ConPTY 的 VT 序列兼容；数据目录 `~/.bosun` 用 `Path.home()` 跨平台。

## 里程碑

**M0 — 平台适配层（后端能在 Windows 起服务）**
- `pty_compat.py`：POSIX 走 ptyprocess，Windows 走 pywinpty；`.cmd` shim 包装；`requirements.txt` 按平台条件依赖 `pywinpty; sys_platform == "win32"`
- `engine_models.py`、`scheduler.py` 锁 shim 接入
- 验收：Windows 上 `python run.py` 启动，`/api/health` 200，前端页面可开，引擎探测显示已装 CLI

**M1 — 任务执行链路（核心验证门槛）**
- 真实跑通：创建任务 → claude/codex 在 ConPTY 执行 → 浏览器终端实时渲染 → 打字插手 → 任务收尾
- **重点验证 waiting_input / idle 启发式**：这些启发式解析终端尾部文本，ConPTY 的转义序列与重绘行为和 POSIX pty 不同，是全项目最大不确定性；不通过则调整启发式的平台分支
- 中断（`\x03`）、winsize 同步、任务终止路径逐项过
- 验收：Windows 上端到端完成一个真实任务，等待/接管/终止行为与 macOS 一致

**M2 — 打包与发布**
- `packaging/bosun.spec` 复用（PyInstaller 跨平台），产物 zip
- release.yml 增加 `windows-2022` job（含启动 + health 冒烟），`_binary_asset_suffix` 增加 win32 → `windows-x86_64.zip`，更新包解压支持 zipfile
- README 安装表补 Windows 行
- 验收：tag 触发三平台产物齐挂 Release

**M3 — 在线自更新（可选，独立排期）**
- 方案：下载解压到暂存 → 写 `update.bat`（等待主进程退出 → robocopy 换目录 → 重启 exe）→ 后端自退出
- 验收：Windows 上从 N 版一键升到 N+1 版

## 风险与应对

| 风险 | 等级 | 应对 |
|---|---|---|
| ConPTY 输出差异破坏等待/空闲启发式 | 高 | 放在 M1 作为通过门槛，预留启发式平台分支；不达标则 Windows 版标注 beta |
| 引擎 CLI 自身的 Windows 兼容性（claude 原生支持较新，omp 依赖 Bun） | 中 | 首版只承诺 claude/codex 双引擎，omp/kimi 标「视上游支持」 |
| 无本机 Windows 环境，调试回路长 | 中 | 开发期用 Windows VM / GitHub Actions 调试；CI 冒烟必须真实起服务 |
| npm `.cmd` shim 经 ConPTY 的参数转义坑 | 低 | `pty_compat` 内集中处理并加单测 |

## 工作量预估

有 Windows 调试环境的前提下：M0 约 1 天，M1 约 1–2 天（启发式调参是弹性项），M2 约 0.5 天，M3 约 1 天。合计 3.5–4.5 天，M1 是关键路径。
