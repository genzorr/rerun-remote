"""Open one RRD in the bundled native desktop viewer."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from urllib.parse import urlsplit, urlunsplit
import uuid

VERSION = "0.39.0"
PORT = 38886
DEFAULT_CONFIG = Path.home() / ".config/rerun-remote/config.toml"
DEFAULT_RESOURCES = {"memory_bytes": 4 * 1024**3, "tasks_max": 128, "runtime_seconds": 7200, "cpu_count": 2}
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class ViewError(RuntimeError):
    """An actionable viewing failure."""


def host_alias(value: str) -> str:
    if not ALIAS.fullmatch(value):
        raise ViewError("Host must be an existing SSH alias using letters, digits, dots, underscores or hyphens, and cannot start with a hyphen.")
    return value


def load_config(path: Path, explicit: bool = False) -> dict:
    try:
        with path.expanduser().open("rb") as source:
            config = tomllib.load(source)
    except FileNotFoundError:
        if explicit:
            raise ViewError(f"Configuration file does not exist: {path}") from None
        config = {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ViewError(f"Cannot read configuration: {error}") from error
    if set(config) - {"hosts", "resources"}:
        raise ViewError("Configuration supports only hosts and resources tables.")
    hosts = config.get("hosts", {})
    resources = config.get("resources", {})
    if not isinstance(hosts, dict) or not isinstance(resources, dict):
        raise ViewError("hosts and resources must be TOML tables.")
    for alias, settings in hosts.items():
        host_alias(alias)
        if not isinstance(settings, dict) or set(settings) - {"recording_dir", "executable"}:
            raise ViewError(f"Host {alias} supports recording_dir and executable only.")
        for key, value in settings.items():
            if not isinstance(value, str) or not value or "\0" in value:
                raise ViewError(f"Host {alias} {key} must be a nonempty path string.")
            if key == "recording_dir" and not value.startswith("/"):
                raise ViewError(f"Host {alias} recording_dir must be an absolute remote directory.")
            if key == "executable" and value != "rerun-remote" and not value.startswith("/"):
                raise ViewError(f"Host {alias} executable must be rerun-remote or an absolute remote executable path.")
    if set(resources) - DEFAULT_RESOURCES.keys():
        raise ViewError("Unknown resource setting; use memory_bytes, tasks_max, runtime_seconds or cpu_count.")
    for key, value in resources.items():
        if type(value) is not int or value < 1:
            raise ViewError(f"Resource {key} must be a positive integer.")
    return {"hosts": hosts, "resources": DEFAULT_RESOURCES | resources}


def selected_file(value: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, ValueError) as error:
        raise ViewError(f"Cannot open recording {value}: {error}") from error
    if not path.is_file() or path.suffix.lower() != ".rrd":
        raise ViewError("Recording must be an existing regular .rrd file.")
    return path


def resolve_target(recording: str, host: str | None, config: dict) -> tuple[str | None, str]:
    if not recording or "\0" in recording:
        raise ViewError("Recording path must be nonempty and cannot contain NUL.")
    if host is None and ":" in recording and not recording.startswith(("/", "./", "../")):
        host, recording = recording.split(":", 1)
        if not recording.startswith("/"):
            raise ViewError("host:path requires an absolute remote recording path; use --host for configured relative paths.")
    if host is None:
        return None, str(selected_file(recording))
    host_alias(host)
    if not recording.startswith("/"):
        directory = config["hosts"].get(host, {}).get("recording_dir")
        if directory is None:
            raise ViewError(f"A relative remote path requires hosts.{host}.recording_dir in configuration.")
        recording = str(Path(directory) / recording)
    return host, recording


def native_binary() -> Path:
    try:
        installed = importlib.metadata.version("rerun-sdk")
        import rerun_cli
    except (ImportError, importlib.metadata.PackageNotFoundError) as error:
        raise ViewError("Rerun is missing; explicitly install or update rerun-remote with uv tool install --force.") from error
    if installed != VERSION:
        raise ViewError(f"Installed Rerun SDK is {installed}; expected {VERSION}. Explicitly reinstall rerun-remote with uv tool install --force on this machine.")
    binary = Path(rerun_cli.__file__).with_name("rerun")
    if sys.platform == "darwin":
        bundled = binary.parent / "Rerun.app" / "Contents" / "MacOS" / "Rerun"
        if bundled.exists():
            binary = bundled
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ViewError(f"Bundled native Rerun executable is missing or not executable: {binary}. Explicitly reinstall rerun-remote with uv tool install --force.")
    try:
        with tempfile.TemporaryDirectory(prefix="rerun-remote-version-") as root:
            result = subprocess.run([str(binary), "--version"], env=isolated_environment(Path(root)), capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise ViewError("Cannot run bundled native Rerun; explicitly reinstall rerun-remote on this machine.") from error
    if not re.search(rf"\brerun(?:-cli)?\s+{re.escape(VERSION)}(?:\s|$)", result.stdout):
        raise ViewError(f"Bundled native Rerun version differs from {VERSION}; explicitly reinstall rerun-remote on this machine.")
    return binary


def isolated_environment(root: Path) -> dict[str, str]:
    environment = dict(os.environ, RERUN_ANALYTICS_ENABLED="false")
    for kind in ("CONFIG", "DATA", "CACHE", "STATE", "RUNTIME"):
        directory = root / kind.lower()
        directory.mkdir(mode=0o700)
        environment[f"XDG_{kind}_HOME" if kind != "RUNTIME" else "XDG_RUNTIME_DIR"] = str(directory)
    return environment


def port_available(port: int = PORT) -> None:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise ViewError(f"Loopback port {port} is occupied. Close its owning session or service and retry; its owner was preserved.") from error


def stop_process(process: subprocess.Popen, *, grace: float = 5) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=5)
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def startup_time(value: str) -> tuple[str, int]:
    match = re.fullmatch(r"([^=\x00]+)=([+-]?[0-9]+)", value)
    if match is None or not match[1].strip():
        raise argparse.ArgumentTypeError("--time requires TIMELINE=INTEGER_NS, with a nonempty timeline and integer nanoseconds.")
    nanoseconds = int(match[2])
    if not -(2**63) < nanoseconds < 2**63:
        raise argparse.ArgumentTypeError("--time requires signed 64-bit nanoseconds, excluding the NaT sentinel -9223372036854775808.")
    return match[1], nanoseconds


def viewer(binary: Path, target: str, threads: int = 2, *, control_port: int | None = None) -> subprocess.Popen:
    return subprocess.Popen([str(binary), "--bind", "127.0.0.1", "--port", str(control_port) if control_port is not None else "auto", "--threads", str(threads), target], start_new_session=True)


def control_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def recording_id(store_id: str) -> str:
    if not store_id.startswith("Recording:"):
        raise ViewError("Viewer returned an invalid recording identity.")
    escaped = False
    for index, character in enumerate(store_id[len("Recording:"):], len("Recording:")):
        if escaped:
            if character not in ("\\", ":"):
                raise ViewError("Viewer returned an invalid recording identity.")
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":" and store_id[index + 1:]:
            return store_id[index + 1:]
    raise ViewError("Viewer returned an invalid recording identity.")


def initialize_time(desktop: subprocess.Popen, port: int, requested: tuple[str, int], *, selected_segment: str | None = None, ssh: subprocess.Popen | None = None, timeout: float = 120) -> None:
    command = [sys.executable, "-m", "rerun_remote", "_initialize", "--viewer-pid", str(desktop.pid), "--control-port", str(port), "--timeline=" + requested[0], "--nanoseconds=" + str(requested[1])]
    if selected_segment is not None:
        command += ["--segment-id=" + selected_segment]
    helper = subprocess.Popen(command, stdout=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if desktop.poll() is not None:
                raise ViewError(f"Native desktop viewer exited before startup time initialization (status {desktop.returncode}).")
            if ssh is not None and ssh.poll() is not None:
                raise ViewError(f"SSH connection ended before startup time initialization (status {ssh.returncode}).")
            status = helper.poll()
            if status is not None:
                if status != 0 or helper.stdout.read(4096).strip() != b'{"initialized": true}':
                    raise ViewError("Could not initialize startup time; check the preceding desktop control diagnostic.")
                return
            time.sleep(0.1)
        raise ViewError("Desktop startup time initialization timed out before recording readiness or exact cursor readback.")
    finally:
        stop_process(helper)
        helper.stdout.close()


def apply_startup_time(viewer_pid: int, port: int, requested: tuple[str, int], *, selected_segment: str | None = None, timeout: float = 120) -> None:
    import numpy as np
    from rerun.experimental import ViewerClient

    timeline_name, nanoseconds = requested
    deadline = time.monotonic() + timeout
    client = None
    selected_store = None
    applied = False
    while time.monotonic() < deadline:
        if not viewer_listener_owned(viewer_pid, port):
            time.sleep(0.1)
            continue
        try:
            if client is None:
                client = ViewerClient.connect(f"rerun+http://127.0.0.1:{port}/proxy")
            state = client.viewer_state()
        except Exception:
            time.sleep(0.1)
            continue
        if state.viewer_version != VERSION:
            raise ViewError(f"Desktop control endpoint reports Rerun {state.viewer_version}; expected {VERSION}. Explicitly update rerun-remote.")
        if state.loading or not state.recordings:
            time.sleep(0.1)
            continue
        if len(state.recordings) != 1:
            raise ViewError("Startup time requires exactly the selected recording in the owned desktop viewer.")
        recording = state.recordings[0]
        identity = recording_id(recording.store_id)
        if selected_segment is not None and identity != selected_segment:
            raise ViewError("Desktop loaded a different recording than the selected remote recording; startup time was not applied.")
        if applied:
            if recording.store_id != selected_store:
                raise ViewError("Desktop recording changed during startup time initialization.")
            if recording.current_timeline == timeline_name and recording.current_time == nanoseconds:
                return
        else:
            timelines = [timeline for timeline in recording.timelines if timeline.name == timeline_name]
            if len(timelines) != 1:
                raise ViewError(f"Requested timeline {timeline_name!r} is unavailable in the selected recording.")
            timeline = timelines[0]
            if timeline.time_type not in ("duration", "timestamp"):
                raise ViewError(f"Timeline {timeline_name!r} is {timeline.time_type}; --time accepts duration or timestamp timelines in nanoseconds.")
            if timeline.start is None or timeline.end is None or not timeline.start <= nanoseconds <= timeline.end:
                raise ViewError(f"Requested nanoseconds are outside the recorded range of timeline {timeline_name!r}.")
            if not viewer_listener_owned(viewer_pid, port):
                raise ViewError("Desktop control listener ownership changed; startup time was not applied.")
            value = np.timedelta64(nanoseconds, "ns") if timeline.time_type == "duration" else np.datetime64(nanoseconds, "ns")
            try:
                client.set_time(timeline_name, **{timeline.time_type: value}, play=False, recording=recording.store_id)
            except Exception as error:
                raise ViewError("Could not initialize the selected recording's startup time.") from error
            selected_store = recording.store_id
            applied = True
        time.sleep(0.1)
    raise ViewError("Desktop startup time initialization timed out before recording readiness or exact cursor readback.")


def local_view(binary: Path, recording: str, requested: tuple[str, int] | None = None) -> int:
    port = control_port() if requested is not None else None
    process = viewer(binary, recording, control_port=port) if requested is not None else viewer(binary, recording)
    try:
        if requested is not None:
            initialize_time(process, port, requested)
        return process.wait()
    finally:
        stop_process(process)


def remote_command(host: str, recording: str, config: dict) -> list[str]:
    host_alias(host)
    executable = config["hosts"].get(host, {}).get("executable", "rerun-remote")
    argv = [executable, "_serve", "--recording", recording]
    for key, value in config["resources"].items():
        argv += ["--" + key.replace("_", "-"), str(value)]
    return ["ssh", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-L", f"127.0.0.1:{PORT}:127.0.0.1:{PORT}", host, shlex.join(argv)]


def readiness(process: subprocess.Popen, timeout: float = 200) -> tuple[str, str | None]:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ViewError(f"Remote startup failed (SSH status {process.returncode}); check the remote diagnostic above and its installed server profile.")
        if select.select([process.stdout], [], [], min(0.2, deadline - time.monotonic()))[0]:
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                raise ViewError("SSH connection closed before remote readiness; check authentication and remote server diagnostics.")
            data.extend(chunk)
            if len(data) > 16384:
                raise ViewError("Remote startup returned an invalid readiness response.")
            if b"\n" in data:
                try:
                    response = json.loads(data.split(b"\n", 1)[0])
                    if response["version"] != VERSION:
                        raise ViewError(f"Remote Rerun version must be {VERSION}; explicitly update rerun-remote on both machines.")
                    url = urlsplit(response["segment_url"])
                    if url.scheme != "rerun+http" or url.hostname != "127.0.0.1" or url.port != PORT or url.username or url.password or not url.path:
                        raise ValueError("unexpected segment address")
                    segment_id = response.get("segment_id")
                    if segment_id is not None and (not isinstance(segment_id, str) or not segment_id):
                        raise ValueError("invalid segment identity")
                    return urlunsplit((url.scheme, f"127.0.0.1:{PORT}", url.path, url.query, url.fragment)), segment_id
                except (ValueError, KeyError, TypeError) as error:
                    raise ViewError("Remote startup returned an invalid readiness response.") from error
    raise ViewError("Remote startup timed out; check SSH authentication, systemd user availability and the selected recording.")


def retire_ssh(process: subprocess.Popen) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    finally:
        stop_process(process)


def remote_view(binary: Path, host: str, recording: str, config: dict, requested: tuple[str, int] | None = None) -> int:
    port_available()
    ssh = subprocess.Popen(remote_command(host, recording, config), stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
    desktop = None
    try:
        segment_url, segment_id = readiness(ssh)
        port = control_port() if requested is not None else None
        if requested is not None and segment_id is None:
            raise ViewError("Remote startup did not identify the selected recording; explicitly update rerun-remote on the execution host.")
        desktop = viewer(binary, segment_url, config["resources"]["cpu_count"], control_port=port) if requested is not None else viewer(binary, segment_url, config["resources"]["cpu_count"])
        if requested is not None:
            initialize_time(desktop, port, requested, selected_segment=segment_id, ssh=ssh)
        while desktop.poll() is None:
            if ssh.poll() is not None:
                raise ViewError(f"SSH connection ended (status {ssh.returncode}); the desktop session has been retired.")
            time.sleep(0.2)
        if desktop.returncode:
            raise ViewError(f"Native desktop viewer exited with status {desktop.returncode}.")
        return 0
    finally:
        if desktop is not None:
            stop_process(desktop)
        retire_ssh(ssh)


def listener_owned(pid: int, port: int = PORT) -> bool:
    address = f"0100007F:{port:04X}"
    try:
        inodes = {line.split()[9] for line in Path("/proc/net/tcp").read_text().splitlines()[1:] if line.split()[1] == address and line.split()[3] == "0A"}
        descriptors = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return False
    for descriptor in descriptors:
        try:
            if os.readlink(descriptor) in {f"socket:[{inode}]" for inode in inodes}:
                return True
        except OSError:
            continue
    return False


def viewer_listener_owned(pid: int, port: int) -> bool:
    if sys.platform == "linux":
        return listener_owned(pid, port)
    if sys.platform == "darwin":
        import psutil
        try:
            connections = psutil.Process(pid).net_connections(kind="tcp")
        except (psutil.Error, OSError, AttributeError) as error:
            raise ViewError("Cannot verify this desktop's loopback control listener ownership; --time cannot safely initialize it.") from error
        return any(connection.status == psutil.CONN_LISTEN and connection.laddr.ip == "127.0.0.1" and connection.laddr.port == port for connection in connections)
    raise ViewError("--time requires observable native desktop listener ownership on Linux or macOS.")


def systemd_properties(unit: str) -> dict[str, str]:
    try:
        result = subprocess.run(["systemctl", "--user", "show", unit, "--property=MemoryMax,TasksMax,RuntimeMaxUSec,ControlGroup"], capture_output=True, text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise ViewError("Cannot inspect session resource controls; a working Linux systemd user manager is required.") from error
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def verify_controls(unit: str, resources: dict, pid: int) -> None:
    properties = systemd_properties(unit)
    if properties.get("MemoryMax") != str(resources["memory_bytes"]) or properties.get("TasksMax") != str(resources["tasks_max"]):
        raise ViewError("Session memory/task resource controls were not applied; remote startup stopped.")
    runtime = properties.get("RuntimeMaxUSec", "")
    expected = resources["runtime_seconds"]
    units = {"d": 86400, "h": 3600, "min": 60, "s": 1, "ms": 0.001, "us": 0.000001}
    parts = re.findall(r"([0-9]+(?:\.[0-9]+)?)(min|ms|us|d|h|s)", runtime)
    if not parts or sum(float(value) * units[suffix] for value, suffix in parts) != expected:
        raise ViewError("Session runtime limit was not applied; remote startup stopped.")
    group = properties.get("ControlGroup")
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text()
        affinity = os.sched_getaffinity(pid)
        memory = (Path("/sys/fs/cgroup") / group.lstrip("/") / "memory.max").read_text().strip() if group else ""
        tasks = (Path("/sys/fs/cgroup") / group.lstrip("/") / "pids.max").read_text().strip() if group else ""
    except OSError as error:
        raise ViewError("Cannot verify effective Linux resource controls for this session.") from error
    if not group or f"0::{group}\n" not in cgroup or memory != str(resources["memory_bytes"]) or tasks != str(resources["tasks_max"]) or len(affinity) != resources["cpu_count"]:
        raise ViewError("Effective session cgroup or CPU affinity differs from its limits; remote startup stopped.")


def catalog_recording(client, selected: Path, disconnected: threading.Event) -> tuple[str, str]:
    def connected():
        if disconnected.is_set():
            raise ViewError("SSH connection closed during startup.")
    connected()
    dataset = client.create_dataset("selected")
    registered = dataset.register([selected.as_uri()]).wait(timeout_secs=60)
    connected()
    if len(registered.segment_ids) != 1 or dataset.segment_ids() != registered.segment_ids:
        raise ViewError("Selected RRD must contain exactly one recording.")
    blueprint = dataset.blueprint_dataset()
    if blueprint is None:
        raise ViewError("Server did not provide its blueprint catalog.")
    saved_ids = blueprint.register([selected.as_uri()]).wait(timeout_secs=60).segment_ids
    connected()
    if len(saved_ids) > 1:
        raise ViewError("Selected RRD contains multiple saved blueprints; select a recording with one saved blueprint.")
    if saved_ids:
        dataset.set_default_blueprint(saved_ids[0])
    fresh = client.get_dataset("selected")
    if fresh.default_blueprint() != (saved_ids[0] if saved_ids else None) or fresh.blueprints() != saved_ids:
        raise ViewError("Saved blueprint did not survive fresh catalog readback.")
    return fresh.segment_url(registered.segment_ids[0]), registered.segment_ids[0]


def serve_worker(args: argparse.Namespace) -> int:
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    resources = {key: getattr(args, key) for key in DEFAULT_RESOURCES}
    binary = native_binary()
    selected = selected_file(args.recording)
    try:
        if importlib.metadata.version("datafusion") != "54.0.0":
            raise ViewError("Remote catalog requires datafusion==54.0.0; explicitly install or update rerun-remote[server].")
        from rerun.catalog import CatalogClient
    except (ImportError, importlib.metadata.PackageNotFoundError) as error:
        raise ViewError("Remote catalog dependencies are missing; explicitly install or update rerun-remote[server].") from error
    verify_controls(args.unit, resources, os.getpid())
    port_available()
    disconnected = threading.Event()
    stop_watcher = threading.Event()
    children = []
    child_lock = threading.Lock()

    def watch_connection():
        descriptor = sys.stdin.fileno()
        while not stop_watcher.is_set():
            if not select.select([descriptor], [], [], 0.2)[0]:
                continue
            if not os.read(descriptor, 4096):
                break
        else:
            return
        disconnected.set()
        with child_lock:
            for child in children:
                stop_process(child)

    watcher = threading.Thread(target=watch_connection, daemon=True)
    with tempfile.TemporaryDirectory(prefix="rerun-remote-server-") as root, tempfile.TemporaryFile() as diagnostics:
        environment = isolated_environment(Path(root))
        with child_lock:
            if disconnected.is_set():
                raise ViewError("SSH connection closed before startup.")
            server = subprocess.Popen([str(binary), "--threads", str(resources["cpu_count"]), "server", "--host", "127.0.0.1", "--port", str(PORT)], env=environment, stdin=subprocess.DEVNULL, stdout=diagnostics, stderr=diagnostics, start_new_session=True)
            children.append(server)
        try:
            watcher.start()
            deadline = time.monotonic() + 60
            client = None
            while time.monotonic() < deadline:
                if disconnected.is_set():
                    raise ViewError("SSH connection closed during startup.")
                if server.poll() is not None:
                    raise ViewError(f"Native remote server exited before readiness (status {server.returncode}).")
                if listener_owned(server.pid):
                    verify_controls(args.unit, resources, server.pid)
                    try:
                        client = CatalogClient(f"rerun+http://127.0.0.1:{PORT}")
                        client.entries()
                        break
                    except Exception:
                        client = None
                time.sleep(0.2)
            if client is None:
                raise ViewError("Native remote server did not become ready within 60 seconds.")
            if not listener_owned(server.pid):
                raise ViewError("Remote loopback listener is not owned by this server; existing catalog preserved.")
            segment_url, segment_id = catalog_recording(client, selected, disconnected)
            if disconnected.is_set() or server.poll() is not None:
                raise ViewError("Remote session ended during recording registration.")
            print(json.dumps({"version": VERSION, "segment_url": segment_url, "segment_id": segment_id}), flush=True)
            while not disconnected.wait(0.2):
                if server.poll() is not None:
                    raise ViewError(f"Native remote server exited (status {server.returncode}).")
            return 0
        finally:
            stop_watcher.set()
            with child_lock:
                stop_process(server)
            watcher.join(timeout=1)


def serve(args: argparse.Namespace) -> int:
    if sys.platform != "linux":
        raise ViewError("Remote serving requires Linux, systemd user services and taskset.")
    selected = selected_file(args.recording)
    resources = {key: getattr(args, key) for key in DEFAULT_RESOURCES}
    if any(value < 1 for value in resources.values()):
        raise ViewError("All resource controls must be positive integers.")
    cpus = sorted(os.sched_getaffinity(0))[:resources["cpu_count"]]
    if len(cpus) != resources["cpu_count"]:
        raise ViewError("Requested CPU count exceeds the execution host's effective CPU affinity.")
    unit = f"rerun-remote-{uuid.uuid4().hex}.scope"
    command = ["systemd-run", "--user", "--scope", "--quiet", "--unit=" + unit, f"--property=MemoryMax={resources['memory_bytes']}", f"--property=TasksMax={resources['tasks_max']}", f"--property=RuntimeMaxSec={resources['runtime_seconds']}s", "taskset", "-c", ",".join(map(str, cpus)), sys.executable, "-m", "rerun_remote", "_worker", "--unit", unit, "--recording", str(selected)]
    for key, value in resources.items():
        command += ["--" + key.replace("_", "-"), str(value)]
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as error:
        raise ViewError("Remote serving requires systemd-run and taskset in the existing execution PATH.") from error
    try:
        return process.wait()
    finally:
        stop_process(process)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, default=None, help="User TOML configuration (default: ~/.config/rerun-remote/config.toml)")
    commands = result.add_subparsers(dest="command", required=True, metavar="{view}")
    view = commands.add_parser("view", help="Open a local or SSH-hosted recording")
    view.add_argument("recording")
    view.add_argument("--host", help="Existing SSH alias; relative paths use its configured recording_dir")
    view.add_argument("--time", type=startup_time, help="Pause at TIMELINE=INTEGER_NS after the selected recording loads")
    view.add_argument("--config", type=Path, default=None, dest="view_config", help="User TOML configuration")
    for name in ("_serve", "_worker"):
        internal = commands.add_parser(name)
        internal.add_argument("--recording", required=True)
        if name == "_worker":
            internal.add_argument("--unit", required=True)
        for key, value in DEFAULT_RESOURCES.items():
            internal.add_argument("--" + key.replace("_", "-"), type=int, default=value)
    initialize = commands.add_parser("_initialize")
    initialize.add_argument("--viewer-pid", type=int, required=True)
    initialize.add_argument("--control-port", type=int, required=True)
    initialize.add_argument("--timeline", required=True)
    initialize.add_argument("--nanoseconds", type=int, required=True)
    initialize.add_argument("--segment-id")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    previous_handlers = {}
    if args.command == "view":
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        for name in ("SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                signum = getattr(signal, name)
                previous_handlers[signum] = signal.signal(signum, interrupt)
    try:
        if args.command == "_initialize":
            requested = startup_time(f"{args.timeline}={args.nanoseconds}")
            apply_startup_time(args.viewer_pid, args.control_port, requested, selected_segment=args.segment_id)
            print(json.dumps({"initialized": True}), flush=True)
            return 0
        if args.command == "_serve":
            return serve(args)
        if args.command == "_worker":
            return serve_worker(args)
        path = args.view_config or args.config or DEFAULT_CONFIG
        config = load_config(path, bool(args.view_config or args.config))
        host, recording = resolve_target(args.recording, args.host, config)
        binary = native_binary()
        if host is None:
            return local_view(binary, recording, args.time) if args.time is not None else local_view(binary, recording)
        return remote_view(binary, host, recording, config, args.time) if args.time is not None else remote_view(binary, host, recording, config)
    except KeyboardInterrupt:
        return 130
    except (ViewError, OSError) as error:
        print(f"rerun-remote: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        if args.command in ("_serve", "_worker", "_initialize"):
            operation = "Desktop startup time initialization" if args.command == "_initialize" else "Remote startup"
            print(f"rerun-remote: {operation} failed ({type(error).__name__}); check the selected recording and the installed Rerun profile.", file=sys.stderr)
            return 1
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
