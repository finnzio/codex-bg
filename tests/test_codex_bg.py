"""Exercise detached launch, caller-specific Herdr layout, and read-only viewing."""

import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "codex-bg"
IS_DARWIN = sys.platform == "darwin"
STUB = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['TEST_TRACE'], 'a') as trace:
    trace.write(json.dumps([name, *args]) + '\\n')
if name == 'herdr':
    if args[:2] == ['pane', 'close'] and os.environ.get('FAIL_CLOSE'):
        sys.exit(1)
    if args[:2] == ['pane', 'split'] and os.environ.get('FAIL_SPLIT'):
        sys.exit(1)
    pane = 'w1:p1' if args[:2] == ['pane', 'current'] else 'w1:p2'
    print(json.dumps({'result': {'pane': {'pane_id': pane, 'tab_id': 'w1:t1'}}}))
'''


@unittest.skipIf(IS_DARWIN, "systemd launch path; macOS coverage is in test_macos.py")
class CodexBgTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-bg-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state"
        self.trace = self.root / "trace.jsonl"
        for name in ("systemctl", "systemd-run", "codex", "herdr"):
            path = self.bin / name
            path.write_text(STUB)
            path.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "TEST_TRACE": str(self.trace),
            "CODEX_BG_STATE_DIR": str(self.state),
            "CODEX_THREAD_ID": "test-thread",
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
        }

    def run_tool(self, *args, **overrides):
        return subprocess.run(
            [str(SCRIPT), *args], cwd=self.root,
            env={**self.env, **overrides}, capture_output=True, text=True, timeout=10,
        )

    def calls(self, command):
        if not self.trace.exists():
            return []
        return [call for line in self.trace.read_text().splitlines()
                if (call := json.loads(line))[0] == command]

    def launch(self, *options, **overrides):
        result = self.run_tool("run", "--name", "test", *options, "--", "printf", "task-marker", **overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = result.stdout.splitlines()[0].removeprefix("started ")
        return unit, result

    def test_launches_task_outside_right_pane_without_stealing_focus(self):
        unit, _ = self.launch()
        self.assertEqual(len(self.calls("systemd-run")), 1)
        self.assertEqual(self.calls("systemd-run")[0][-2:], ["printf", "task-marker"])
        split = next(call for call in self.calls("herdr") if call[1:3] == ["pane", "split"])
        self.assertEqual(split[3:], ["--current", "--direction", "right", "--cwd", str(self.root), "--no-focus"])
        view = next(call for call in self.calls("herdr") if call[1:3] == ["pane", "run"])
        self.assertEqual(shlex.split(view[-1]), ["env", f"CODEX_BG_STATE_DIR={self.state}", str(SCRIPT), "watch", unit, "w1:p2"])
        self.assertNotIn("task-marker", view[-1])

    def test_outside_herdr_and_explicit_opt_out_never_control_panes(self):
        self.launch(HERDR_ENV="0")
        self.launch("--no-pane")
        self.assertEqual(self.calls("herdr"), [])

    def test_missing_caller_never_uses_the_focused_pane(self):
        self.launch(HERDR_PANE_ID="")
        self.assertEqual(self.calls("herdr"), [])

    def test_split_failure_does_not_fail_or_restart_the_task(self):
        _, result = self.launch(FAIL_SPLIT="1")
        self.assertEqual(len(self.calls("systemd-run")), 1)
        self.assertIn("background task is still running", result.stderr)

    def test_existing_viewer_is_reused(self):
        unit, _ = self.launch()
        result = self.run_tool("pane", unit)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already open", result.stdout)
        self.assertEqual(sum(call[1:3] == ["pane", "split"] for call in self.calls("herdr")), 1)

    def test_existing_legacy_job_gains_reusable_pane_metadata(self):
        self.state.mkdir()
        unit = "codex-bg-legacy-20260101-000000"
        (self.state / (unit + ".status")).write_text("state=running\n")
        (self.state / (unit + ".log")).write_text("legacy progress\n")
        self.assertEqual(self.run_tool("pane", unit).returncode, 0)
        self.assertIn("already open", self.run_tool("pane", unit).stdout)
        self.assertEqual(sum(call[1:3] == ["pane", "split"] for call in self.calls("herdr")), 1)

    def test_viewer_reads_custom_log_path_without_shell_evaluation(self):
        log = self.root / "logs" / "$(touch INJECTED) progress.log"
        unit, _ = self.launch("--no-pane", "--log", str(log))
        log.write_text("actual progress 73%\n")
        (self.state / (unit + ".status")).write_text("state=finished exit=0\n")
        result = self.run_tool("watch", unit)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("actual progress 73%", result.stdout)
        self.assertFalse((self.root / "INJECTED").exists())
        self.assertFalse(self.calls("herdr"))

    def test_list_is_concise_and_does_not_dump_runner_commands(self):
        self.state.mkdir()
        for i in range(25):
            (self.state / f"job-{i}.status").write_text("state=finished exit=0\nQueued message detail\n")
        result = self.run_tool("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 20)
        self.assertNotIn("Queued", result.stdout)
        self.assertEqual(self.calls("systemctl"), [])

    def test_automatic_viewer_closes_its_exact_pane_for_all_terminal_states(self):
        unit, _ = self.launch()
        for status in ("state=finished exit=0", "state=finished exit=1", "state=stopped"):
            with self.subTest(status=status):
                self.trace.write_text("")
                (self.state / (unit + ".status")).write_text(status + "\n")
                result = self.run_tool("watch", unit, "w1:p2", HERDR_PANE_ID="w1:p2")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls("herdr"), [["herdr", "pane", "close", "w1:p2"]])

    def test_viewer_refuses_to_close_a_different_pane(self):
        unit, _ = self.launch("--no-pane")
        (self.state / (unit + ".status")).write_text("state=finished exit=0\n")
        result = self.run_tool("watch", unit, "w1:p2")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls("herdr"), [])

    def test_close_failure_does_not_change_job_result_or_fail_the_viewer(self):
        unit, _ = self.launch()
        status = self.state / (unit + ".status")
        status.write_text("state=finished exit=1\n")
        result = self.run_tool("watch", unit, "w1:p2", HERDR_PANE_ID="w1:p2", FAIL_CLOSE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(status.read_text(), "state=finished exit=1\n")

    def test_rejects_paths_as_unit_ids(self):
        result = self.run_tool("watch", "../../other-file")
        self.assertEqual(result.returncode, 2)


@unittest.skipUnless(os.environ.get("CODEX_BG_LIVE_HERDR") == "1", "opt-in live Herdr/systemd check")
@unittest.skipUnless(sys.platform.startswith("linux"), "live systemd/Herdr checks need Linux pidfd and user systemd")
class CodexBgLiveTests(unittest.TestCase):
    def test_finished_job_automatically_closes_only_its_progress_pane(self):
        with tempfile.TemporaryDirectory(prefix="codex-bg-live-close-") as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            codex = binary / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n")
            codex.chmod(0o755)
            state = root / "state"
            env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                   "CODEX_THREAD_ID": "local-smoke-test", "CODEX_BG_STATE_DIR": str(state)}
            result = subprocess.run(
                [str(SCRIPT), "run", "--name", "auto-close-" + uuid.uuid4().hex[:8], "--",
                 "bash", "-c", "sleep 3; printf 'auto-close-finished\\n'"],
                cwd=root, env=env, capture_output=True, text=True, check=True, timeout=15,
            )
            unit = result.stdout.splitlines()[0].removeprefix("started ")
            pane = json.loads((state / (unit + ".json")).read_text())["pane_id"]
            try:
                # Bounded integration-test wait; no model polling or production watcher.
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    found = subprocess.run(["herdr", "pane", "get", pane], capture_output=True, timeout=3)
                    if found.returncode != 0:
                        break
                    time.sleep(0.2)
                else:
                    self.fail("progress pane stayed open after completion")
                self.assertIn("state=finished exit=0", (state / (unit + ".status")).read_text())
                self.assertIn("auto-close-finished", (state / (unit + ".log")).read_text())
                subprocess.run(["herdr", "pane", "get", env["HERDR_PANE_ID"]],
                               capture_output=True, check=True, timeout=3)
            finally:
                subprocess.run(["herdr", "pane", "close", pane], capture_output=True)
                subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)

    def test_closing_progress_pane_does_not_interrupt_task_or_completion(self):
        self.assertEqual(os.environ.get("HERDR_ENV"), "1")
        with tempfile.TemporaryDirectory(prefix="codex-bg-live-") as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            wake = root / "wake"
            codex = binary / "codex"
            codex.write_text("#!/bin/bash\nprintf 'queued\\n' > " + shlex.quote(str(wake)) + "\n")
            codex.chmod(0o755)
            state = root / "state"
            env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                   "CODEX_THREAD_ID": "local-smoke-test", "CODEX_BG_STATE_DIR": str(state)}
            result = subprocess.run(
                [str(SCRIPT), "run", "--name", "pane-smoke-" + uuid.uuid4().hex[:8], "--mem", "128M", "--",
                 "bash", "-c", "printf 'smoke-start\\n'; sleep 6; printf 'smoke-after-pane-close\\n'; sleep 1"],
                cwd=root, env=env, capture_output=True, text=True, check=True, timeout=15,
            )
            unit = result.stdout.splitlines()[0].removeprefix("started ")
            metadata = json.loads((state / (unit + ".json")).read_text())
            pane = metadata["pane_id"]
            closed = False
            try:
                subprocess.run(["herdr", "pane", "wait-output", pane, "--match", "smoke-start", "--timeout", "5000"],
                               env=env, capture_output=True, check=True, timeout=8)
                pid = int(subprocess.check_output(["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"], text=True))
                # pidfd_open is Linux-only; this class is already skipped off Linux.
                fd = os.pidfd_open(pid)
                try:
                    subprocess.run(["herdr", "pane", "close", pane], env=env, capture_output=True, check=True, timeout=5)
                    closed = True
                    subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], check=True)
                    ready, _, _ = select.select([fd], [], [], 20)
                    self.assertTrue(ready, "detached runner did not finish")
                finally:
                    os.close(fd)
                self.assertIn("smoke-after-pane-close", Path(metadata["log"]).read_text())
                self.assertIn("state=finished exit=0", (state / (unit + ".status")).read_text())
                self.assertTrue(wake.is_file(), "completion was not delivered after pane closure")
            finally:
                if not closed:
                    subprocess.run(["herdr", "pane", "close", pane], env=env, capture_output=True)
                subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)


if __name__ == "__main__":
    unittest.main()
