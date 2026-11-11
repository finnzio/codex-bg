---
name: codex-bg
description: Run long, noninteractive commands outside a Codex turn and queue completion back to the originating thread. Use for builds, full test suites, imports, and batch jobs expected to take several minutes. Select optional hc progress terminals or herdr-tui panes from the current runtime context. Do not use for dev servers, interactive commands, or short checks.
---

# codex-bg

Use the installed `codex-bg` executable. If it is not on PATH, check
`${CODEX_HOME:-$HOME/.codex}/bin/codex-bg`. If missing, report the dependency;
do not replace it with an ad-hoc background runner.

## Launch

Run from the task's working directory, with `CODEX_THREAD_ID` supplied by the
active Codex session. Confirm `codex queue --help` is available when setting up
a new environment. On Linux, user systemd is the supported mode for cancellation
and memory limits; the setsid fallback has fewer controls. On macOS, the
launchd backend (Python 3 helper) supports cancellation and duplicate-name
checks; **`--mem` is unsupported and errors**. macOS support is verified on
GitHub Actions `macos-latest`, not on a personal Mac.

```bash
codex-bg run --name build --heavy --mem 8G -- make build
codex-bg run --name tests --heavy -- bash -c 'PATH=$PWD/.venv/bin:$PATH make test'
codex-bg run --name import --env API_TOKEN -- ./scripts/import-data
```

Choose the command and memory limit to match the project. `--heavy` limits
participating jobs to two concurrent slots per state directory. Avoid overlapping
jobs that write the same build output or database. Pass secret environment
variable names with `--env KEY`, never their values in command arguments.
Commands must not write secrets to their output.
For shell syntax, prefer `bash -c` to preserve the launcher's PATH. A login
shell (`bash -lc`) may hide the Node.js/pnpm toolchain by resetting PATH.

After launching, report the returned unit and log path, then **end the turn**.
Do not keep the turn alive with sleeps, repeated status checks, or `tail -f`.
The detached runner and optional progress viewer do not need model polling.

## Completion and status

On a `[codex-bg] ... finished` message, read the named log tail, distinguish the
command's exit code from notification delivery, and continue the original task.
Do not rerun merely because a delayed notification arrived. Compare it with
already-handled results. Rerun only after a relevant failure or subsequent change.

```bash
codex-bg list
codex-bg stop <unit>
codex-bg pane <unit>
codex-bg watch <unit>
```

Use `list` once for an explicit progress request or a missing completion message.
Cancellation via `stop` requires user systemd (Linux) or launchd (macOS) and
does not send a completion wake.
Queue acceptance does not prove the session resumed: a closed session may retain
the message until reopened. Preserve the log/status paths when notification fails.

## Identify the control plane

Before the first long command in a session, inspect the current shell context;
repeat after changing terminals or resuming in another environment:

```bash
printf 'HC_TERMINAL_ID=%s HERDR_ENV=%s HERDR_PANE_ID=%s\n' "${HC_TERMINAL_ID:-}" "${HERDR_ENV:-}" "${HERDR_PANE_ID:-}"
```

Use the first matching context:

| Context | Action |
| --- | --- |
| Nonempty `HC_TERMINAL_ID` | Read `hc --skill` and validate `hc get "$HC_TERMINAL_ID"`; hc takes precedence over herdr-tui variables |
| No hc context, `HERDR_ENV=1` and nonempty `HERDR_PANE_ID` | Read `herdr --skill`; use the herdr-tui viewer |
| Neither | Keep detached execution and logs; do not select another client's terminal or pane |

Installed commands do not establish the active control plane. If hc caller
validation fails, use `--no-pane` and report unavailable progress viewing;
do not fall back to a herdr-tui session. Keep using `codex-bg` for the task
in every environment.

## hc terminals

With `HC_TERMINAL_ID`, `run` opens only a read-only `watch` child terminal.
Use its returned `hc attach <terminal>` command to view progress. hc does
not arrange windows. Codex inside hc must start with `--no-daemon`.

Let the helper create and reuse its viewer; do not launch the workload through
`hc run` or register a second completion watcher for the progress terminal.
The independent runner still delivers completion through `codex queue`.
Closing the viewer does not cancel the task. After success, failure or explicit
stop, event waits collect its exit and remove the automatic terminal. Match
worker addresses carefully if also using `hc wait --children`: log viewers
are children too. Manual `watch` never closes the caller's terminal.

## herdr-tui panes

With `HERDR_ENV=1` and `HERDR_PANE_ID`, `run` opens a progress pane to the right
of the caller in the same tab, preserving focus, when there is no hc context. Read `herdr --skill` before
direct pane operations. Let the helper create its viewer; do not create a second
pane or run the workload in that pane.

The pane contains only the log viewer. Closing it or interrupting the viewer
does not cancel the task. The automatic viewer closes its own pane by ID when
it observes completion or explicit cancellation; logs remain on disk. Manual
`watch` does not close the caller's terminal. `--no-pane` disables automatic
viewing. Pane failures must not trigger a task restart.
