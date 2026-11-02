# codex-bg

[中文](README.zh-CN.md) · [How it works](docs/how-it-works.md) · [Agent skill](skills/codex-bg/SKILL.md)

Run a long command outside the active Codex turn, then queue its result back to
the same thread. Optional Herdr panes show real logs and close automatically
when the task finishes. The background wait does not require model polling.

```bash
codex-bg run --name test --heavy --mem 8G -- make test
```

The launcher prints the unit and log paths. Codex can end its turn while the job
runs; the runner sends a completion message with exit code, duration, and log path.
This project is an independent helper, not an official OpenAI product.

## Requirements

- **Linux** (primary): Bash 4+, GNU coreutils, and util-linux (`flock`, `setsid`).
  A working **user systemd** manager is recommended. `systemd-run` must support
  `--expand-environment=no`. Check `systemctl --user is-system-running` and
  `systemd-run --help`. A `degraded` user manager is also accepted. Linux under
  WSL may work when user systemd is enabled.
- **macOS** (launchd backend): Bash (the system 3.2 shell is enough for the
  launcher), **Python 3.9+**, and `launchctl`. Jobs are registered as temporary
  launchd services. **`--mem` is not supported** and exits with an error.
  macOS support is **not personally Mac-tested**; verification is the GitHub
  Actions `macos-latest` job (unit tests plus a live launchd smoke with a stub
  `codex`). Windows is not supported.
- A `codex` executable that supports `codex queue --thread … --message …`, plus
  `CODEX_THREAD_ID` from the active session. Check **`codex queue --help`** first:
  not every Codex build exposes this command. The helper does not add it.
- **jq** is recommended for metadata and custom log paths, and required for panes.
- Optional **Herdr** with `HERDR_ENV=1`, `HERDR_PANE_ID`, and the `pane current`,
  `split`, `get`, `rename`, `run`, and `close` CLI commands. Pane integration has
  been exercised with Herdr 0.9.5.
- Tests use Python 3.9+. Live Linux Herdr tests use `os.pidfd_open` (Linux only).

On Linux, no Python dependency is required to run the helper. On macOS, Python 3
runs the launchd backend (`bin/codex-bg-macos.py`), which `install.sh` copies next
to the launcher.

## Install

```bash
git clone https://github.com/finnzio/codex-bg.git
cd codex-bg
./install.sh
export PATH="${CODEX_HOME:-$HOME/.codex}/bin:$PATH"
```

The installer copies the script to `$CODEX_HOME/bin` and the skill to
`$CODEX_HOME/skills/codex-bg`; `CODEX_HOME` defaults to `~/.codex`. It does not
edit shell profiles, agent instructions, or install system packages. Existing
files with those names are updated. Start a new Codex session if the skill is
not discovered by the current one.

```bash
./install.sh --bin-dir "$HOME/.local/bin" --skill-dir "$HOME/.agents/skills"
./install.sh --no-skill
```

Invoke the skill as `$codex-bg`, or let Codex select it for a long task. The skill
explains when to launch, when to end the turn, and how to handle completion.

To update, pull the repository and rerun the installer. To uninstall, remove the
installed `codex-bg` file and `codex-bg` skill directory after stopping active jobs.

## Commands

| Command | Behavior |
| --- | --- |
| `run [options] -- command [args…]` | Launch a detached job from the current directory |
| `list` | Show up to 20 recent status files |
| `stop UNIT` | Stop a systemd/launchd job, mark it stopped, and send no completion wake |
| `pane UNIT` | Open/reuse a Herdr progress pane in the caller's tab |
| `watch UNIT` | Follow the log in the current terminal without closing it |

| Run option | Meaning |
| --- | --- |
| `--name NAME` | Short label; sanitized and timestamped into a unit name |
| `--heavy` | Acquire one of two slots shared by the state directory |
| `--mem 8G` | Limit the command's systemd scope; swap limit is zero (Linux only; errors on macOS) |
| `--env KEY` | Forward an environment variable by name to the runner; repeatable |
| `--log FILE` | Write command output to a custom path; install jq to use it with `watch` |
| `--no-pane` | Disable automatic Herdr pane creation |

```bash
codex-bg run --name build --heavy --mem 8G -- make build
codex-bg run --name import --env API_TOKEN -- ./scripts/import-data
codex-bg run --name check --no-pane -- bash -lc 'PATH=$PWD/.venv/bin:$PATH make check'
codex-bg list
# Use the exact unit returned by run/list:
codex-bg stop codex-bg-check-20261102-120000
```

Pass commands as arguments after `--`. Use an explicit shell only when shell
syntax is needed. Pass secret **names** with `--env`, not secret values in argv.
Output is saved verbatim: the helper does not redact secrets printed by commands.

## Herdr progress panes

The pane opens **to the right of the calling pane, in its current tab**, without
stealing focus. It runs a log viewer, never the workload. Closing the pane leaves
the background job running. When the viewer observes success, failure, or an
explicit stop, it closes **its own pane ID**; logs remain available on disk.
Manual `watch` never closes your terminal. If you interrupt the automatic viewer,
it no longer monitors completion or closes the pane. Pane errors do not restart
or cancel the task.

## State and limitations

State defaults to `/tmp/codex-bg`; set `CODEX_BG_STATE_DIR` to use another directory.
Each job has a `.log`, `.status`, and (with jq) `.json` metadata file. Choose a
private, user-owned state directory and restrictive umask on shared machines.
The default directory is temporary and can be removed on reboot.

- Completion uses `codex queue`, with three delivery attempts. Accepted messages
  may remain queued while a session is busy or closed; acceptance does not prove
  the model resumed. This helper does not pass `--resume` or configure remote routing.
- Without user systemd on Linux, `setsid` + `nohup` still detach execution, but
  **memory limits, duplicate-name protection, and the `stop` command are not
  supported**. Prefer systemd for managed Linux jobs. The fallback inherits the
  launcher's environment.
- On macOS, launchd provides duplicate-name checks and `stop`. There is no
  systemd-style memory cap (`--mem` is rejected). Heavy slots use Python
  `fcntl.flock` instead of the `flock` CLI.
- This is not a durable scheduler: no reboot recovery, automatic job retry,
  exactly-once notification, or hostile-code sandbox is provided. Do not launch
  commands with conflicting build outputs or database writes concurrently.
- Use it for noninteractive jobs, not dev servers or commands that need a TTY.
  In systemd mode only selected environment variables are forwarded; use `--env`
  for additional configuration.

## Test

```bash
bash -n bin/codex-bg && bash -n install.sh
python3 -m unittest discover -s tests -v
# In a Linux Herdr pane with user systemd:
CODEX_BG_LIVE_HERDR=1 python3 -m unittest discover -s tests -v
```

Unit tests stub external commands (including a fake `launchctl` for the macOS
backend). Opt-in live Linux tests create temporary systemd jobs and panes,
verify auto-close and that manual pane closure leaves the job running, then
clean them up. They replace the completion receiver with a local stub; they do
not send messages to a real Codex thread. GitHub Actions runs the unit suite on
`ubuntu-latest` and `macos-latest`; the macOS job also runs a live launchd smoke
with a stub `codex`. Neither job exercises a live Codex or Herdr session.

## License

[MIT](LICENSE).
