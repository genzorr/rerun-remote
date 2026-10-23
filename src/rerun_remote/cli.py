"""Open a recording or existing live stream in the native desktop viewer."""

from __future__ import annotations

import argparse
import importlib.metadata
import ipaddress
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
from urllib.parse import urlsplit, urlunsplit
import uuid

VERSION = "0.40.0"
PORT = 38886
DEFAULT_RESOURCES = {"memory_bytes": 4 * 1024**3, "tasks_max": 128, "runtime_seconds": 7200, "cpu_count": 2}
CLIENT_INSTALL = "uv tool install --python 3.12 --reinstall --upgrade 'rerun-remote @ git+https://github.com/genzorr/rerun-remote.git@main'"
SERVER_REPAIR = "Remote runtime is invalid; retry using a fresh isolated XDG_CACHE_HOME, or explicitly update the laptop client."
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class ViewError(RuntimeError):
    """An actionable viewing failure."""


def host_alias(value: str) -> str:
    if not ALIAS.fullmatch(value):
        raise ViewError("Host must be an existing SSH alias using letters, digits, dots, underscores or hyphens, and cannot start with a hyphen.")
    return value


def selected_file(value: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, ValueError) as error:
        raise ViewError(f"Cannot open recording {value}: {error}") from error
    if not path.is_file() or path.suffix.lower() != ".rrd":
        raise ViewError("Recording must be an existing regular .rrd file.")
    return path


def live_url(value: str) -> str:
    try:
        url = urlsplit(value)
        loopback = url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback
        if url.scheme != "rerun+http" or not loopback or url.port is None or not 1 <= url.port <= 65535 or url.username is not None or url.password is not None or url.fragment or "#" in value or url.path != "/proxy" or any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("unsupported proxy address")
    except (ValueError, TypeError) as error:
        raise ViewError("Live target must be rerun+http://LOOPBACK:PORT/proxy, without credentials or fragments, with port 1–65535.") from error
    return value


def resolve_target(target: str) -> tuple[str | None, str, bool]:
    if not target or "\0" in target:
        raise ViewError("Target must be nonempty and cannot contain NUL.")
    if target.startswith("rerun+") or ("://" in target and target.split(":", 1)[0] in ("http", "https", "rerun")):
        return None, live_url(target), True
    if ":" in target and not target.startswith(("/", "./", "../")):
        host, target = target.split(":", 1)
        host_alias(host)
        if "://" in target:
            return host, live_url(target), True
        if not target.startswith("/"):
            raise ViewError("Remote recording paths must be absolute: SSH_ALIAS:/absolute/path/recording.rrd.")
        if Path(target).suffix.lower() != ".rrd":
            raise ViewError("Remote recording must be a .rrd file.")
        return host, target, False
    return None, str(selected_file(target)), False


def native_binary(*, server: bool = False) -> Path:
    remedy = SERVER_REPAIR if server else f"Explicitly install or update with: {CLIENT_INSTALL}"
    try:
        installed = importlib.metadata.version("rerun-sdk")
        import rerun_cli
    except (ImportError, importlib.metadata.PackageNotFoundError) as error:
        raise ViewError(f"Rerun is missing. {remedy}") from error
    if installed != VERSION:
        raise ViewError(f"Installed Rerun SDK is {installed}; expected {VERSION}. {remedy}")
    binary = Path(rerun_cli.__file__).with_name("rerun")
    if sys.platform == "darwin":
        bundled = binary.parent / "Rerun.app" / "Contents" / "MacOS" / "Rerun"
        if bundled.exists():
            binary = bundled
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ViewError(f"Bundled native Rerun executable is missing or not executable: {binary}. {remedy}")
    try:
        with tempfile.TemporaryDirectory(prefix="rerun-remote-version-") as root:
            result = subprocess.run([str(binary), "--version"], env=isolated_environment(Path(root)), capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise ViewError(f"Cannot run bundled native Rerun. {remedy}") from error
    if not re.search(rf"\brerun(?:-cli)?\s+{re.escape(VERSION)}(?:\s|$)", result.stdout):
        raise ViewError(f"Bundled native Rerun version differs from {VERSION}. {remedy}")
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


def initialize_time(desktop: subprocess.Popen, port: int, requested: tuple[str, int] | None, *, selected_segment: str | None = None, ssh: subprocess.Popen | None = None, timeout: float = 120, live: bool = False) -> None:
    command = [sys.executable, "-m", "rerun_remote", "_initialize" if requested is not None else "_check-live", "--viewer-pid", str(desktop.pid), "--control-port", str(port)]
    if requested is not None:
        command += ["--timeline=" + requested[0], "--nanoseconds=" + str(requested[1])]
        if live:
            command += ["--live"]
    if selected_segment is not None:
        command += ["--segment-id=" + selected_segment]
    operation = "live endpoint readiness" if live else "startup time initialization"
    helper = subprocess.Popen(command, stdout=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if desktop.poll() is not None:
                raise ViewError(f"Native desktop viewer exited before {operation} (status {desktop.returncode}).")
            if ssh is not None and ssh.poll() is not None:
                raise ViewError(f"SSH connection ended before {operation} (status {ssh.returncode}).")
            status = helper.poll()
            if status is not None:
                if status != 0 or helper.stdout.read(4096).strip() != b'{"initialized": true}':
                    raise ViewError(f"Could not complete {operation}; check the preceding desktop control diagnostic.")
                return
            time.sleep(0.1)
        raise ViewError(f"Desktop {operation} timed out before recording readiness or exact cursor readback.")
    finally:
        stop_process(helper)
        helper.stdout.close()


def apply_startup_time(viewer_pid: int, port: int, requested: tuple[str, int] | None, *, selected_segment: str | None = None, timeout: float = 120, live: bool = False) -> None:
    import numpy as np
    from rerun.experimental import ViewerClient

    timeline_name, nanoseconds = requested if requested is not None else (None, None)
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
        if (state.loading and not live) or not state.recordings:
            time.sleep(0.1)
            continue
        if requested is None:
            return
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
            if live and not timelines:
                time.sleep(0.1)
                continue
            if len(timelines) != 1:
                raise ViewError(f"Requested timeline {timeline_name!r} is unavailable in the selected recording.")
            timeline = timelines[0]
            if timeline.time_type not in ("duration", "timestamp"):
                raise ViewError(f"Timeline {timeline_name!r} is {timeline.time_type}; --time accepts duration or timestamp timelines in nanoseconds.")
            if live and (timeline.start is None or timeline.end is None or nanoseconds > timeline.end):
                time.sleep(0.1)
                continue
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
    raise ViewError("Desktop live endpoint readiness timed out waiting for stream data or requested nanoseconds." if live else "Desktop startup time initialization timed out before recording readiness or exact cursor readback.")


def local_view(binary: Path, recording: str, requested: tuple[str, int] | None = None) -> int:
    port = control_port() if requested is not None else None
    process = viewer(binary, recording, control_port=port) if requested is not None else viewer(binary, recording)
    try:
        if requested is not None:
            initialize_time(process, port, requested)
        return process.wait()
    finally:
        stop_process(process)


def ssh_command(host: str, local_port: int, remote_port: int, remote_host: str = "127.0.0.1") -> list[str]:
    host_alias(host)
    destination = f"[{remote_host}]" if ":" in remote_host else remote_host
    # A shared master would own the forward outside this process's cleanup group.
    return ["ssh", "-S", "none", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-L", f"127.0.0.1:{local_port}:{destination}:{remote_port}", host]


def remote_command(host: str, recording: str, resources: dict, port: int = PORT) -> list[str]:
    argv = ["_serve", "--recording", recording, "--port", str(port)]
    for key, value in resources.items():
        argv += ["--" + key.replace("_", "-"), str(value)]
    from . import _bootstrap
    source = Path(_bootstrap.__file__).read_text()
    command = "command -v python3 >/dev/null 2>&1 || { printf '%s\\n' 'rerun-remote: remote setup requires Python 3.10 or later (python3 in PATH).' >&2; exit 127; }; exec python3 -I -c " + shlex.quote(source) + " " + shlex.join(argv[1:])
    return ssh_command(host, port, port) + [command]


def readiness(process: subprocess.Popen, timeout: float = 200, *, port: int = PORT) -> tuple[str, str | None]:
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
                        raise ViewError(f"Remote Rerun version must be {VERSION}; explicitly update the laptop client.")
                    if not isinstance(response["segment_url"], str):
                        raise ValueError("invalid segment address")
                    url = urlsplit(response["segment_url"])
                    if url.scheme != "rerun+http" or url.hostname != "127.0.0.1" or url.port != port or url.username is not None or url.password is not None or url.fragment or not url.path:
                        raise ValueError("unexpected segment address")
                    segment_id = response.get("segment_id")
                    if segment_id is not None and (not isinstance(segment_id, str) or not segment_id):
                        raise ValueError("invalid segment identity")
                    return urlunsplit((url.scheme, f"127.0.0.1:{port}", url.path, url.query, "")), segment_id
                except (ValueError, KeyError, TypeError) as error:
                    raise ViewError("Remote startup returned an invalid readiness response.") from error
    raise ViewError("Remote startup timed out; check SSH authentication, systemd user availability and the selected recording.")


def retire_ssh(process: subprocess.Popen) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except (BrokenPipeError, BlockingIOError):
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    finally:
        stop_process(process)


def remote_view(binary: Path, host: str, recording: str, resources: dict, requested: tuple[str, int] | None = None, *, port: int = PORT) -> int:
    port_available(port)
    ssh = subprocess.Popen(remote_command(host, recording, resources, port), stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
    desktop = None
    try:
        from . import _bootstrap
        try:
            _bootstrap.transfer(ssh, _bootstrap.source_payload())
        except _bootstrap.SetupError as error:
            raise ViewError(str(error)) from error
        segment_url, segment_id = readiness(ssh, timeout=_bootstrap.SETUP_SECONDS + 200, port=port)
        control = control_port() if requested is not None else None
        if requested is not None and segment_id is None:
            raise ViewError("Remote startup did not identify the selected recording; retry with a fresh remote cache or update the laptop client.")
        desktop = viewer(binary, segment_url, resources["cpu_count"], control_port=control) if requested is not None else viewer(binary, segment_url, resources["cpu_count"])
        if requested is not None:
            initialize_time(desktop, control, requested, selected_segment=segment_id, ssh=ssh)
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
        ssh.stdout.close()


def live_view(binary: Path, target: str, *, host: str | None = None, port: int = PORT, requested: tuple[str, int] | None = None) -> int:
    ssh = None
    desktop = None
    try:
        if host is not None:
            port_available(port)
            url = urlsplit(target)
            command = ssh_command(host, port, url.port, url.hostname)
            command.insert(1, "-N")
            ssh = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + 20
            while not viewer_listener_owned(ssh.pid, port):
                if ssh.poll() is not None:
                    raise ViewError(f"SSH tunnel failed (status {ssh.returncode}); check the preceding SSH diagnostic.")
                if time.monotonic() >= deadline:
                    raise ViewError("SSH tunnel did not establish its owned loopback listener within 20 seconds.")
                time.sleep(0.1)
            target = urlunsplit((url.scheme, f"127.0.0.1:{port}", url.path, url.query, ""))
        control = control_port()
        desktop = viewer(binary, target, control_port=control)
        initialize_time(desktop, control, requested, ssh=ssh, live=True)
        while desktop.poll() is None:
            if ssh is not None and ssh.poll() is not None:
                raise ViewError(f"SSH connection ended (status {ssh.returncode}); the desktop session has been retired.")
            time.sleep(0.2)
        if desktop.returncode:
            raise ViewError(f"Native desktop viewer exited with status {desktop.returncode}.")
        return 0
    finally:
        if desktop is not None:
            stop_process(desktop)
        if ssh is not None:
            stop_process(ssh)


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
    verify_controls(args.unit, resources, os.getpid())
    binary = native_binary(server=True)
    selected = selected_file(args.recording)
    try:
        if importlib.metadata.version("datafusion") != "54.0.0":
            raise ViewError(f"Remote catalog requires datafusion==54.0.0. {SERVER_REPAIR}")
        from rerun.catalog import CatalogClient
    except (ImportError, importlib.metadata.PackageNotFoundError) as error:
        raise ViewError(f"Remote catalog dependencies are missing. {SERVER_REPAIR}") from error
    verify_controls(args.unit, resources, os.getpid())
    port_available(args.port)
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
            server = subprocess.Popen([str(binary), "--threads", str(resources["cpu_count"]), "server", "--host", "127.0.0.1", "--port", str(args.port)], env=environment, stdin=subprocess.DEVNULL, stdout=diagnostics, stderr=diagnostics, start_new_session=True)
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
                if listener_owned(server.pid, args.port):
                    verify_controls(args.unit, resources, server.pid)
                    try:
                        client = CatalogClient(f"rerun+http://127.0.0.1:{args.port}")
                        client.entries()
                        break
                    except Exception:
                        client = None
                time.sleep(0.2)
            if client is None:
                raise ViewError("Native remote server did not become ready within 60 seconds.")
            if not listener_owned(server.pid, args.port):
                raise ViewError("Remote loopback listener is not owned by this server; existing catalog preserved.")
            segment_url, segment_id = catalog_recording(client, selected, disconnected)
            if disconnected.is_set() or server.poll() is not None:
                raise ViewError("Remote session ended during recording registration.")
            print(json.dumps({"version": VERSION, "segment_url": segment_url, "segment_id": segment_id}), flush=True)
            while not disconnected.wait(0.2):
                if server.poll() is not None:
                    raise ViewError(f"Native remote server exited (status {server.returncode}).")
            return 0
        except Exception as error:
            diagnostics.seek(0, os.SEEK_END)
            size = diagnostics.tell()
            diagnostics.seek(max(0, size - 4096))
            excerpt = diagnostics.read(4096).decode("utf-8", errors="replace").strip()
            if excerpt:
                raise ViewError(f"{error}\nNative server diagnostic (last 4096 bytes):\n{excerpt}") from error
            raise
        finally:
            stop_watcher.set()
            with child_lock:
                stop_process(server)
            watcher.join(timeout=1)


def server_preflight(args: argparse.Namespace) -> tuple[Path, list[int]]:
    if sys.platform != "linux":
        raise ViewError("Remote serving requires Linux, systemd user services and taskset.")
    selected = selected_file(args.recording)
    resources = {key: getattr(args, key) for key in DEFAULT_RESOURCES}
    if any(value < 1 for value in resources.values()):
        raise ViewError("All resource controls must be positive integers.")
    cpus = sorted(os.sched_getaffinity(0))[:resources["cpu_count"]]
    if len(cpus) != resources["cpu_count"]:
        raise ViewError("Requested CPU count exceeds the execution host's effective CPU affinity.")
    port_available(args.port)
    return selected, cpus


def scope_command(args: argparse.Namespace, worker: list[str]) -> tuple[str, list[str]]:
    _, cpus = server_preflight(args)
    resources = {key: getattr(args, key) for key in DEFAULT_RESOURCES}
    unit = f"rerun-remote-{uuid.uuid4().hex}.scope"
    command = ["systemd-run", "--user", "--scope", "--quiet", "--unit=" + unit, f"--property=MemoryMax={resources['memory_bytes']}", f"--property=TasksMax={resources['tasks_max']}", f"--property=RuntimeMaxSec={resources['runtime_seconds']}s", "taskset", "-c", ",".join(map(str, cpus)), *worker]
    return unit, command


def serve(args: argparse.Namespace) -> int:
    selected, _ = server_preflight(args)
    resources = {key: getattr(args, key) for key in DEFAULT_RESOURCES}
    worker = [sys.executable, "-m", "rerun_remote", "_worker", "--unit", "UNIT", "--recording", str(selected), "--port", str(args.port)]
    for key, value in resources.items():
        worker += ["--" + key.replace("_", "-"), str(value)]
    unit, command = scope_command(args, worker)
    command[command.index("--unit") + 1] = unit
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as error:
        raise ViewError("Remote serving requires systemd-run and taskset in the existing execution PATH.") from error
    try:
        return process.wait()
    finally:
        stop_process(process)


def positive_integer(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(value)


def port_number(value: str) -> int:
    port = positive_integer(value)
    if port > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parser(command: str | None = None) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    if command in ("_serve", "_worker"):
        result.add_argument("--recording", required=True)
        result.add_argument("--port", type=port_number, default=PORT)
        if command == "_worker":
            result.add_argument("--unit", required=True)
        for key, value in DEFAULT_RESOURCES.items():
            result.add_argument("--" + key.replace("_", "-"), type=positive_integer, default=value)
    elif command in ("_initialize", "_check-live"):
        result.add_argument("--viewer-pid", type=int, required=True)
        result.add_argument("--control-port", type=port_number, required=True)
        if command == "_initialize":
            result.add_argument("--timeline", required=True)
            result.add_argument("--nanoseconds", type=int, required=True)
            result.add_argument("--segment-id")
            result.add_argument("--live", action="store_true")
    else:
        result.add_argument("target", help="Local .rrd, SSH_ALIAS:/absolute/file.rrd, or [SSH_ALIAS:]rerun+http://LOOPBACK:PORT/proxy")
        result.add_argument("--time", type=startup_time, help="Pause once at TIMELINE=INTEGER_NS after the recording loads")
        result.add_argument("--port", type=port_number, default=None, help=f"Local SSH forwarding port; also the remote file server port (default {PORT})")
        for key in DEFAULT_RESOURCES:
            result.add_argument("--" + key.replace("_", "-"), type=positive_integer, default=None, help="Remote file server resource limit")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments.pop(0) if arguments and arguments[0] in ("_serve", "_worker", "_initialize", "_check-live") else None
    args = parser(command).parse_args(arguments)
    previous_handlers = {}
    if command is None:
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        for name in ("SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                signum = getattr(signal, name)
                previous_handlers[signum] = signal.signal(signum, interrupt)
    try:
        if command == "_initialize":
            requested = startup_time(f"{args.timeline}={args.nanoseconds}")
            apply_startup_time(args.viewer_pid, args.control_port, requested, selected_segment=args.segment_id, live=args.live)
            print(json.dumps({"initialized": True}), flush=True)
            return 0
        if command == "_check-live":
            apply_startup_time(args.viewer_pid, args.control_port, None, live=True)
            print(json.dumps({"initialized": True}), flush=True)
            return 0
        if command == "_serve":
            return serve(args)
        if command == "_worker":
            return serve_worker(args)
        host, target, live = resolve_target(args.target)
        if host is None and args.port is not None:
            raise ViewError("--port applies only to SSH targets.")
        if (host is None or live) and any(getattr(args, key) is not None for key in DEFAULT_RESOURCES):
            raise ViewError("Resource options apply only to remote files.")
        binary = native_binary()
        if live:
            return live_view(binary, target, host=host, port=args.port or PORT, requested=args.time)
        if host is None:
            return local_view(binary, target, args.time) if args.time is not None else local_view(binary, target)
        resources = {key: getattr(args, key) or value for key, value in DEFAULT_RESOURCES.items()}
        return remote_view(binary, host, target, resources, args.time, port=args.port or PORT)
    except KeyboardInterrupt:
        return 130
    except (ViewError, OSError) as error:
        print(f"rerun-remote: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        if command is not None:
            operation = "Desktop startup initialization" if command in ("_initialize", "_check-live") else "Remote startup"
            print(f"rerun-remote: {operation} failed ({type(error).__name__}: {str(error)[:1000]}); check the selected recording and the installed Rerun profile.", file=sys.stderr)
            return 1
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
