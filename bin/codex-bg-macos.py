"""macOS launchd backend. Python 3.9+, standard library only.

Not exercised on a personal Mac; GitHub Actions macos-latest is the verification
target. Override the launchctl binary with CODEX_BG_LAUNCHCTL for tests.
"""

import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
import time


class DuplicateJob(ValueError):
    """A same-name launchd job is still active."""


def stamp():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def write_status(path, value):
    temporary = path.with_suffix(".status.tmp")
    temporary.write_text(value + "\n")
    temporary.replace(path)


def launchctl(*args):
    binary = os.environ.get("CODEX_BG_LAUNCHCTL", "launchctl")
    return subprocess.run([binary, *args], capture_output=True, text=True, timeout=40)


def domain():
    # GUI sessions and headless/SSH CI runners need different bootstrap domains.
    gui = f"gui/{os.getuid()}"
    return gui if launchctl("print", gui).returncode == 0 else f"user/{os.getuid()}"


def launch(state, unit, name, thread, codex, cwd, log, heavy, *arguments):
    separator = arguments.index("--")
    keys, command = arguments[:separator], arguments[separator + 1:]
    if not command:
        raise ValueError("no command after --")
    state = Path(state).resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Serialize same-name admission. Runner owns the separate two-slot heavy lock.
    with (state / f"name.{name}.lock").open("a") as admission:
        fcntl.flock(admission, fcntl.LOCK_EX)
        for file in state.glob("*.json"):
            metadata = json.loads(file.read_text())
            if metadata.get("backend") != "launchd" or metadata.get("name") != name:
                continue
            service = metadata["service"]
            status_file = file.with_suffix(".status")
            status = status_file.read_text().splitlines()[0] if status_file.exists() else ""
            if status.startswith(("state=starting", "state=running", "state=waiting-heavy-slot")):
                if launchctl("print", service).returncode == 0:
                    raise DuplicateJob(f"a job named '{name}' is already running")
            else:
                # Completed launchd registrations are removed on same-name reuse.
                launchctl("bootout", service)
        config_path = state / f"{unit}.launch.json"
        status_path = state / f"{unit}.status"
        if status_path.exists():
            raise ValueError("unit timestamp collision; choose another name or retry next second")
        namespace = domain()
        digest = hashlib.sha256(str(state).encode()).hexdigest()[:12]
        label = f"io.github.finnzio.codex-bg.{digest}.{unit}"
        service = f"{namespace}/{label}"
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", *keys) if key in os.environ}
        env["PYTHONUNBUFFERED"] = "1"
        config = dict(unit=unit, name=name, thread=thread, codex=codex, cwd=cwd,
                      log=log, heavy=heavy == "1", state=str(state), command=command)
        config_path.write_text(json.dumps(config))
        metadata = dict(log=log, cwd=cwd, backend="launchd", name=name, service=service)
        (state / f"{unit}.json").write_text(json.dumps(metadata))
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text("")
        write_status(status_path, "state=starting")
        plist_path = state / f"{unit}.plist"
        plist = dict(Label=label, RunAtLoad=True, KeepAlive=False, ExitTimeOut=25,
                     ProgramArguments=[sys.executable, str(Path(__file__).resolve()), "runner", str(config_path)],
                     WorkingDirectory=cwd, EnvironmentVariables=env,
                     StandardOutPath=str(state / f"{unit}.runner.log"),
                     StandardErrorPath=str(state / f"{unit}.runner.log"))
        with plist_path.open("wb") as handle:
            plistlib.dump(plist, handle)
        result = launchctl("bootstrap", namespace, str(plist_path))
        if result.returncode:
            write_status(status_path, "state=launch-failed")
            raise ValueError(f"launchctl bootstrap failed: {result.stderr.strip()}")


def runner(config_path):
    config = json.loads(Path(config_path).read_text())
    state = Path(config["state"])
    status_path = state / f'{config["unit"]}.status'
    stopped = False
    child = None

    def request_stop(signum, frame):
        nonlocal stopped
        stopped = True
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    slot = None
    try:
        if config["heavy"]:
            write_status(status_path, "state=waiting-heavy-slot")
            while slot is None and not stopped:
                for number in (1, 2):
                    candidate = (state / f"heavy.{number}.lock").open("a")
                    try:
                        fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        slot = candidate
                        break
                    except BlockingIOError:
                        candidate.close()
                if slot is None:
                    time.sleep(0.2)
        if stopped:
            return
        write_status(status_path, f"state=running started={stamp()}")
        started = time.monotonic()
        with open(config["log"], "ab", buffering=0) as log:
            try:
                child = subprocess.Popen(config["command"], cwd=config["cwd"], stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                # Handle a stop delivered between the check and Popen.
                if stopped:
                    request_stop(None, None)
                deadline = None
                while child.poll() is None:
                    if stopped:
                        deadline = deadline or time.monotonic() + 15
                        if time.monotonic() >= deadline:
                            os.killpg(child.pid, signal.SIGKILL)
                    time.sleep(0.1)
                code = child.wait()
                code = code if code >= 0 else 128 - code
            except OSError as error:
                log.write(f"codex-bg: command failed to start ({error.__class__.__name__})\n".encode())
                code = 127
        if stopped:
            return
        duration = int(time.monotonic() - started)
        write_status(status_path, f"state=finished exit={code} duration={duration}s finished={stamp()}")
        message = (f'[codex-bg] {config["name"]} finished: exit={code} duration={duration}s '
                   f'log={config["log"]} unit={config["unit"]}. Read the end of the log (tail/rg), '
                   'then continue the task. Do not re-run it unless the log shows it must be.')
        for attempt in range(3):
            if stopped:
                return
            with status_path.open("a") as output:
                try:
                    sent = subprocess.run([config["codex"], "queue", "--thread", config["thread"],
                                           "--message", message], stdout=output, stderr=subprocess.STDOUT, timeout=30)
                    if sent.returncode == 0:
                        return
                except (OSError, subprocess.TimeoutExpired):
                    output.write("wake=attempt-failed\n")
            time.sleep(5)
        with status_path.open("a") as output:
            output.write("wake=failed\n")
    finally:
        if stopped:
            # Kill any grandchildren that ignored TERM, including when their parent exited first.
            if child is not None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            current = status_path.read_text() if status_path.exists() else ""
            if not current.startswith("state=finished"):
                write_status(status_path, f"state=stopped stopped={stamp()}")
        if slot is not None:
            slot.close()


def stop(state, unit):
    state = Path(state).resolve()
    metadata = json.loads((state / f"{unit}.json").read_text())
    if metadata.get("backend") != "launchd":
        raise ValueError("job was not started by the launchd backend")
    service = metadata["service"]
    result = launchctl("bootout", service)
    if result.returncode and launchctl("print", service).returncode == 0:
        raise ValueError(f"launchctl bootout failed: {result.stderr.strip()}")
    status_path = state / f"{unit}.status"
    if not status_path.read_text().startswith("state=finished"):
        write_status(status_path, f"state=stopped stopped={stamp()}")
    # The plist can contain explicitly forwarded secrets. Remove it after unloading.
    (state / f"{unit}.plist").unlink(missing_ok=True)
    (state / f"{unit}.launch.json").unlink(missing_ok=True)
    print(f"stopped {unit} (no wake message is sent for a stopped job)")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        action, *args = sys.argv[1:]
        if action in ("launch", "stop"):
            unit = args[1]
            if not re.fullmatch(r"codex-bg-[A-Za-z0-9_-]+-[0-9]{8}-[0-9]{6}", unit):
                raise ValueError("invalid unit name")
        {"launch": launch, "stop": stop, "runner": runner}[action](*args)
    except DuplicateJob as error:
        print(f"codex-bg: {error}", file=sys.stderr)
        sys.exit(3)
    except (ValueError, OSError, subprocess.TimeoutExpired, KeyError, IndexError) as error:
        print(f"codex-bg: {error}", file=sys.stderr)
        sys.exit(2)
