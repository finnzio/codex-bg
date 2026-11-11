# codex-bg

[English](README.md) · [运行原理](docs/how-it-works.zh-CN.md) · [Agent 技能](skills/codex-bg/SKILL.md)

让长任务在 Codex 回合之外运行，结束后通过 `codex queue` 把结果送回原线程。
等待期间不需要模型反复查询状态。在 hc 中可自动创建进度终端，在 herdr-tui 中可打开进度窗格，
显示真实日志，并在任务结束后清理查看器。

这是独立社区工具，不是 OpenAI 官方产品。

## 安装

- **Linux（主路径）**：Bash 4+、GNU coreutils、util-linux（`flock`、`setsid`）。
  推荐启用 user systemd；`systemd-run` 需支持 `--expand-environment=no`。
  WSL 需另行确认 user systemd 可用。
- **macOS（launchd）**：系统自带 Bash 3.2 即可启动器；需要 **Python 3.9+** 与
  `launchctl`。任务以临时 launchd 服务登记。**不支持 `--mem`**，传入会报错退出。
  macOS 支持**未在个人真机验证**，以 GitHub Actions `macos-latest`（单测 + 真实
  launchd 冒烟，`codex` 用桩）为准。不支持 Windows。
- 推荐安装 jq；hc 和 herdr-tui 查看器需要 jq。
- 可选 herdr-core（`hc`）：有 `HC_TERMINAL_ID` 时使用原生终端；Codex 需以 `--no-daemon` 启动。
  Linux 已用 hc 0.1.20 验证。无 hc 上下文时，可用 herdr-tui（`herdr`）的 pane 命令；
  两种环境都没有也能跑后台任务。

**先执行 `codex queue --help`。** 当前 Codex 必须提供 `queue --thread --message`，
且活动会话需注入 `CODEX_THREAD_ID`。不是每个 Codex 构建都有该命令，本工具不会补装它。

Linux 运行助手本身不依赖 Python。macOS 由 `bin/codex-bg-macos.py` 驱动 launchd，
`install.sh` 会把它与启动器装到同一目录。

```bash
git clone https://github.com/finnzio/codex-bg.git
cd codex-bg
./install.sh
export PATH="${CODEX_HOME:-$HOME/.codex}/bin:$PATH"
```

默认将脚本安装到 `$CODEX_HOME/bin/codex-bg`，技能安装到
`$CODEX_HOME/skills/codex-bg`；未设置 `CODEX_HOME` 时使用 `~/.codex`。
安装器只更新这几个文件，不修改 shell profile、AGENTS.md 或安装系统软件。
技能未被当前会话发现时，启动新会话。

```bash
./install.sh --bin-dir "$HOME/.local/bin" --skill-dir "$HOME/.agents/skills"
./install.sh --no-skill
```

更新：拉取仓库后重新运行安装器。卸载：先停止活动任务，再删除所安装的脚本和技能目录。

## 用法

在 Codex 会话中调用 `$codex-bg`，也可以直接执行：

```bash
codex-bg run --name test --heavy --mem 8G -- make test
codex-bg run --name import --env API_TOKEN -- ./scripts/import-data
codex-bg run --name check --no-pane -- bash -c 'PATH=$PWD/.venv/bin:$PATH make check'
codex-bg list
codex-bg pane <unit>
codex-bg watch <unit>
codex-bg stop <unit>
```

`<unit>` 替换成启动输出中的完整任务名。
依赖启动器 PATH 的命令使用 `bash -c`；`bash -lc` 会读取登录配置，可能重置 PATH，
导致找不到 Node.js 或 pnpm 等工具。

| 选项 | 用途 |
| --- | --- |
| `--name` | 任务短名，附加时间戳生成 unit |
| `--heavy` | 在同一状态目录中限制重任务最多两个并发 |
| `--mem` | systemd 子 scope 的内存上限，禁用该 scope 的 swap（仅 Linux；macOS 上报错） |
| `--env KEY` | 按变量名向 runner 转发环境变量，可重复 |
| `--log FILE` | 自定义日志路径，查看器需要 jq 读取元数据 |
| `--no-pane` | 不自动创建 hc 终端或 herdr-tui 窗格 |

启动后，Agent 报告 unit 和日志路径并结束回合。任务结束时，runner 写入退出码和耗时，
再向原线程排入消息；Agent 收到消息后检查日志、继续原任务。不要用模型循环等结果。

## 控制平面与进度查看器

按当前 shell 的环境变量选择，不凭是否安装命令判断。两种上下文都存在时优先 hc。

| 上下文 | 查看器 |
| --- | --- |
| `HC_TERMINAL_ID` 非空 | hc 只读子终端，用返回的 `hc attach <终端>` 查看 |
| 无 hc 上下文，且 `HERDR_ENV=1`、`HERDR_PANE_ID` 非空 | herdr-tui 当前 tab 的右侧 pane |
| 两者都没有 | 仅后台执行与日志文件 |

hc 模式先验证调用者，再通过 `hc run --bg --rm --no-notify --parent <调用者>` 启动
`codex-bg watch`。复用时校验查看器的存活状态、父终端和任务标签。独立的 `hc wait`
收集器等待退出，遇到超时或消息提前返回时续接；任务成功、失败或显式停止后清理查看器。
hc 管理终端，窗口布局由查看应用负责。Linux 收集器使用 `setsid`/`nohup`；
macOS 使用 `nohup`/`disown`，不依赖 `setsid`。

任务仍在独立的 systemd/launchd runner 中运行。关闭进度终端不会取消任务；查看器不另发
hc 完成消息，任务仍由 `codex queue` 唤醒。hc 调用者无效或查看器创建失败时保留后台任务与日志，
不回退到控制另一个 herdr-tui 会话。已有任务可用 `codex-bg pane <unit>` 创建或复用查看器。

### herdr-tui 窗格

需要 `HERDR_ENV=1`、`HERDR_PANE_ID`，以及 herdr-tui pane 命令。已在 herdr-tui 0.9.5 上实测。

- 在调用者所在 tab 右侧创建，不抢焦点。
- pane 中只有日志查看器，任务由独立 runner 执行。
- 提前关闭 pane 不会取消任务；取消用 `codex-bg stop`。
- 查看器观察到任务成功、失败或停止后，只按自己的 pane ID 关闭；日志仍在磁盘。
- 手动 `watch` 不关闭当前终端；自动查看器被中断后，也不再负责自动关闭。
- pane 创建或关闭失败不改变任务结果，不重启任务。

## 边界

默认状态目录为 `/tmp/codex-bg`，可通过 `CODEX_BG_STATE_DIR` 改到其他位置。
保存日志、状态，以及 jq 可用时的 JSON 元数据。在共享机器上选择用户私有目录并设置
严格的 umask。日志原样保存，不会自动脱敏；不要让命令输出密钥。

Linux 推荐 systemd：无 user systemd 时退回 `setsid` + `nohup`，仍可后台执行和发送完成消息，
但**没有内存限制、重名任务拦截或可用的 `stop` 命令**。退回模式会继承启动环境。
macOS 用 launchd 提供重名检查与 `stop`；没有 systemd 式内存上限（`--mem` 直接拒绝）。
`--heavy` 槽位在 macOS 上用 Python `fcntl.flock`，不用 `flock` 命令。

`codex queue` 接受消息不等于会话已经恢复；关闭或忙碌的会话可能暂存消息。
发送失败会重试三次，最终标记 `wake=failed`；不保证恰好一次通知。
不支持重启恢复、持久调度或任务自动重试，也不提供恶意代码沙箱。
它适合构建、测试和批处理，不适合 dev server、交互命令或需要 TTY 的任务。

## 验证

```bash
bash -n bin/codex-bg && bash -n install.sh
python3 -m unittest discover -s tests -v
CODEX_BG_LIVE_HERDR=1 python3 -m unittest discover -s tests -v
CODEX_BG_LIVE_HC=1 python3 -B -m unittest discover -s tests -v
```

需要 Python 3.9+。普通测试使用命令替身（含假 `launchctl`）；可选的 Linux 真实测试需要
对应的 hc/herdr-tui 环境和 user systemd，会创建并清理临时任务与查看器。
hc 验证覆盖任务成功、失败、显式停止、查看器自动清理和关闭查看器不影响任务。
完成消息接收器使用本地替身，
不给真实 Codex 会话发消息。GitHub Actions 在 `ubuntu-latest` 与 `macos-latest` 上跑单测；
macOS job 另跑一次真实 launchd 冒烟（stub `codex`）。macOS 的 hc 查看器设置有命令替身测试，
本机真实 hc 集成验证仅在 Linux 运行；不把单测或 CI 当成真实 Codex/hc/herdr-tui 验收。

许可证：[MIT](LICENSE)。
