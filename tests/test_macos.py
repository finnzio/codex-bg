"""Unit tests for the launchd backend. launchctl is replaced with a PATH stub."""

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "bin" / "codex-bg-macos.py"
SCRIPT = ROOT / "bin" / "codex-bg"

LAUNCHCTL_STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, plistlib, subprocess, sys
trace = pathlib.Path(os.environ["TEST_TRACE"])
args = sys.argv[1:]
trace.open("a").write(json.dumps(["launchctl", *args]) + "\n")
state = pathlib.Path(os.environ["LAUNCHCTL_STATE"])
active = state / "active.json"
loaded = json.loads(active.read_text()) if active.exists() else {}
if args[:1] == ["print"]:
    target = args[1]
    if "/" not in target or target.count("/") == 1 and target.split("/")[0] in ("gui", "user"):
        # Domain probe (gui/UID or user/UID): always succeed so tests use gui/.
        sys.exit(0)
    sys.exit(0 if target in loaded else 1)
if args[:1] == ["bootstrap"]:
    namespace, plist_path = args[1], args[2]
    with open(plist_path, "rb") as handle:
        plist = plistlib.load(handle)
    label = plist["Label"]
    service = f"{namespace}/{label}"
    log = plist.get("StandardOutPath")
    env = os.environ.copy()
    env.update(plist.get("EnvironmentVariables") or {})
    out = open(log, "ab") if log else subprocess.DEVNULL
    child = subprocess.Popen(
        plist["ProgramArguments"], cwd=plist.get("WorkingDirectory") or os.getcwd(),
        env=env, stdout=out, stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
        start_new_session=True,
    )
    loaded[service] = {"plist": plist_path, "label": label, "pid": child.pid}
    active.write_text(json.dumps(loaded))
    sys.exit(0)
if args[:1] == ["bootout"]:
    service = args[1]
    entry = loaded.pop(service, None)
    active.write_text(json.dumps(loaded))
    if entry and entry.get("pid"):
        try:
            os.killpg(entry["pid"], 15)
        except ProcessLookupError:
            try:
                os.kill(entry["pid"], 15)
            except ProcessLookupError:
                pass
    sys.exit(0)
sys.stderr.write("launchctl-stub: unsupported " + " ".join(args) + "\n")
sys.exit(1)
'''


def load_helper():
    spec = importlib.util.spec_from_file_location("codex_bg_macos", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MacosHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-bg-macos-")
        self.addCleanup(self._cleanup_temp)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.trace = self.root / "trace.jsonl"
        self.launchctl_state = self.root / "launchctl-state"
        self.launchctl_state.mkdir()
        launchctl = self.bin / "launchctl"
        launchctl.write_text(LAUNCHCTL_STUB)
        launchctl.chmod(0o755)
        codex = self.bin / "codex"
        codex.write_text("#!/bin/sh\nprintf 'queued\\n'\n")
        codex.chmod(0o755)
        uname = self.bin / "uname"
        uname.write_text("#!/bin/sh\necho Darwin\n")
        uname.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "TEST_TRACE": str(self.trace),
            "LAUNCHCTL_STATE": str(self.launchctl_state),
            "CODEX_BG_LAUNCHCTL": str(launchctl),
            "CODEX_BG_STATE_DIR": str(self.state),
            "CODEX_THREAD_ID": "mac-thread",
            "HERDR_ENV": "0",
            "HC_TERMINAL_ID": "",
        }
        # Direct helper.launch calls read process env, not subprocess env.
        previous = {key: os.environ.get(key) for key in (
            "CODEX_BG_LAUNCHCTL", "TEST_TRACE", "LAUNCHCTL_STATE")}
        os.environ["CODEX_BG_LAUNCHCTL"] = str(launchctl)
        os.environ["TEST_TRACE"] = str(self.trace)
        os.environ["LAUNCHCTL_STATE"] = str(self.launchctl_state)

        def restore_env():
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore_env)
        self.helper = load_helper()

    def _cleanup_temp(self):
        # Runner children may briefly hold files after bootout; retry removal.
        active = self.launchctl_state / "active.json"
        if active.exists():
            for entry in json.loads(active.read_text()).values():
                pid = entry.get("pid")
                if not pid:
                    continue
                try:
                    os.killpg(pid, 9)
                except ProcessLookupError:
                    try:
                        os.kill(pid, 9)
                    except ProcessLookupError:
                        pass
        last_error = None
        for _ in range(20):
            try:
                self.temp.cleanup()
                return
            except OSError as error:
                last_error = error
                time.sleep(0.05)
        if last_error:
            raise last_error

    def calls(self, command="launchctl"):
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text().splitlines()
                if json.loads(line)[0] == command]

    def wait_status(self, unit, prefix, timeout=8):
        path = self.state / f"{unit}.status"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.read_text().startswith(prefix):
                return path.read_text()
            time.sleep(0.05)
        self.fail(f"{unit} never reached {prefix!r}: {path.read_text() if path.exists() else '<missing>'}")

    def test_launch_writes_plist_status_and_finishes_with_wake(self):
        unit = "codex-bg-demo-20260101-120000"
        log = self.state / f"{unit}.log"
        self.helper.launch(
            str(self.state), unit, "demo", "mac-thread", str(self.bin / "codex"),
            str(self.root), str(log), "0", "--", "printf", "mac-ok\n",
        )
        status = self.wait_status(unit, "state=finished exit=0")
        self.assertIn("duration=", status)
        self.assertIn("mac-ok", log.read_text())
        self.assertIn("queued", status)
        metadata = json.loads((self.state / f"{unit}.json").read_text())
        self.assertEqual(metadata["backend"], "launchd")
        self.assertEqual(metadata["name"], "demo")
        with (self.state / f"{unit}.plist").open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertTrue(plist["RunAtLoad"])
        self.assertFalse(plist["KeepAlive"])
        self.assertEqual(plist["ProgramArguments"][0], sys.executable)
        self.assertEqual(plist["ProgramArguments"][2], "runner")
        self.assertEqual(plist["WorkingDirectory"], str(self.root))
        bootstraps = [call for call in self.calls() if call[1] == "bootstrap"]
        self.assertEqual(len(bootstraps), 1)

    def test_command_argv_with_spaces_is_preserved(self):
        unit = "codex-bg-space-20260101-120001"
        log = self.state / f"{unit}.log"
        marker = self.root / "out file.txt"
        self.helper.launch(
            str(self.state), unit, "space", "mac-thread", str(self.bin / "codex"),
            str(self.root), str(log), "0", "--",
            "bash", "-c", 'printf hi > "out file.txt"',
        )
        self.wait_status(unit, "state=finished exit=0")
        self.assertEqual(marker.read_text(), "hi")

    def test_duplicate_name_while_running_is_rejected(self):
        unit = "codex-bg-hold-20260101-120002"
        log = self.state / f"{unit}.log"
        self.helper.launch(
            str(self.state), unit, "hold", "mac-thread", str(self.bin / "codex"),
            str(self.root), str(log), "0", "--", "bash", "-c", "sleep 5",
        )
        self.wait_status(unit, "state=running")
        with self.assertRaises(self.helper.DuplicateJob):
            self.helper.launch(
                str(self.state), "codex-bg-hold-20260101-120003", "hold", "mac-thread",
                str(self.bin / "codex"), str(self.root), str(self.state / "other.log"),
                "0", "--", "true",
            )
        self.helper.stop(str(self.state), unit)
        self.assertTrue((self.state / f"{unit}.status").read_text().startswith("state=stopped"))
        self.assertFalse((self.state / f"{unit}.plist").exists())

    def test_heavy_jobs_serialize_on_two_slots(self):
        started = self.root / "heavy-started"
        release = self.root / "heavy-release"
        started.write_text("")
        units = []
        for index in range(3):
            unit = f"codex-bg-heavy{index}-20260101-13000{index}"
            units.append(unit)
            log = self.state / f"{unit}.log"
            self.helper.launch(
                str(self.state), unit, f"heavy{index}", "mac-thread", str(self.bin / "codex"),
                str(self.root), str(log), "1", "--",
                "bash", "-c",
                f'printf x >> "{started}"; while [ ! -f "{release}" ]; do sleep 0.05; done',
            )
        deadline = time.monotonic() + 5
        waiting = []
        while time.monotonic() < deadline:
            if started.read_text().count("x") >= 2:
                waiting = [
                    unit for unit in units
                    if (self.state / f"{unit}.status").read_text().startswith("state=waiting-heavy-slot")
                ]
                if waiting:
                    break
            time.sleep(0.05)
        self.assertEqual(started.read_text().count("x"), 2)
        self.assertEqual(len(waiting), 1, {
            unit: (self.state / f"{unit}.status").read_text().splitlines()[0] for unit in units
        })
        release.write_text("go")
        for unit in units:
            self.wait_status(unit, "state=finished exit=0", timeout=10)

    def test_runner_records_nonzero_exit(self):
        unit = "codex-bg-fail-20260101-140000"
        config = self.state / f"{unit}.launch.json"
        log = self.state / f"{unit}.log"
        log.write_text("")
        (self.state / f"{unit}.status").write_text("state=starting\n")
        config.write_text(json.dumps({
            "unit": unit, "name": "fail", "thread": "mac-thread",
            "codex": str(self.bin / "codex"), "cwd": str(self.root),
            "log": str(log), "heavy": False, "state": str(self.state),
            "command": ["bash", "-c", "exit 7"],
        }))
        self.helper.runner(str(config))
        status = (self.state / f"{unit}.status").read_text()
        self.assertTrue(status.startswith("state=finished exit=7"), status)

    def test_shell_mem_rejected_when_uname_reports_darwin(self):
        result = subprocess.run(
            [str(SCRIPT), "run", "--name", "mem", "--mem", "8G", "--", "true"],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not supported on macOS", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_shell_run_stop_through_stubbed_launchctl(self):
        result = subprocess.run(
            [str(SCRIPT), "run", "--name", "via-shell", "--no-pane", "--",
             "bash", "-c", "printf shell-ok; sleep 4"],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = result.stdout.splitlines()[0].removeprefix("started ")
        self.wait_status(unit, "state=running")
        stop = subprocess.run(
            [str(SCRIPT), "stop", unit],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertIn("stopped", stop.stdout)
        self.assertTrue((self.state / f"{unit}.status").read_text().startswith("state=stopped"))


@unittest.skipUnless(sys.platform == "darwin", "live launchd only on macOS runners")
class MacosLiveLaunchdTests(unittest.TestCase):
    def test_real_launchd_run_finish_and_stop(self):
        with tempfile.TemporaryDirectory(prefix="codex-bg-live-mac-") as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            wake = root / "wake"
            codex = binary / "codex"
            codex.write_text("#!/bin/sh\nprintf 'queued\\n' >> " + str(wake) + "\n")
            codex.chmod(0o755)
            state = root / "state"
            env = {
                **os.environ,
                "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                "CODEX_THREAD_ID": "live-mac",
                "CODEX_BG_STATE_DIR": str(state),
                "HERDR_ENV": "0",
                "HC_TERMINAL_ID": "",
            }
            finished = subprocess.run(
                [str(SCRIPT), "run", "--name", "liveok", "--no-pane", "--",
                 "bash", "-c", "printf live-ok\\n"],
                cwd=root, env=env, capture_output=True, text=True, check=True, timeout=15,
            )
            unit = finished.stdout.splitlines()[0].removeprefix("started ")
            deadline = time.monotonic() + 20
            status_path = state / f"{unit}.status"
            while time.monotonic() < deadline:
                if status_path.exists() and status_path.read_text().startswith("state=finished exit=0"):
                    break
                time.sleep(0.1)
            else:
                self.fail(status_path.read_text() if status_path.exists() else "missing status")
            self.assertIn("live-ok", (state / f"{unit}.log").read_text())
            self.assertTrue(wake.exists())

            held = subprocess.run(
                [str(SCRIPT), "run", "--name", "livestop", "--no-pane", "--",
                 "bash", "-c", "sleep 30"],
                cwd=root, env=env, capture_output=True, text=True, check=True, timeout=15,
            )
            stop_unit = held.stdout.splitlines()[0].removeprefix("started ")
            deadline = time.monotonic() + 10
            stop_status = state / f"{stop_unit}.status"
            while time.monotonic() < deadline:
                if stop_status.exists() and stop_status.read_text().startswith("state=running"):
                    break
                time.sleep(0.1)
            stopped = subprocess.run(
                [str(SCRIPT), "stop", stop_unit],
                cwd=root, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertTrue(stop_status.read_text().startswith("state=stopped"))


if __name__ == "__main__":
    unittest.main()
