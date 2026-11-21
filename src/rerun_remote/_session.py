"""One native viewer and one owned catalog connection per SSH alias."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from . import _bootstrap, cli

REQUEST_SOCKET = "RERUN_REMOTE_OPEN_SOCKET"


def installed_viewer() -> Path | None:
    root = Path.home() / ".local/share/rerun-remote/viewer" / cli.VERSION
    identity = root / "remote-open-v1"
    if not identity.is_file():
        return None
    binary = root / "Rerun.app/Contents/MacOS/Rerun"
    cli.verify_native_binary(binary, "Reinstall the matching in-app SSH Viewer.")
    with binary.open("rb") as executable:
        actual = hashlib.file_digest(executable, "sha256").hexdigest()
    if actual != identity.read_text().strip():
        raise cli.ViewError("The in-app SSH Viewer differs from its installation receipt. Reinstall the matching build.")
    return binary


@dataclass
class Catalog:
    process: subprocess.Popen
    port: int

    def read(self, timeout: float, check: Callable[[], None], *, registration: bool = False) -> tuple[str, str]:
        url, segment = cli.readiness(
            self.process, timeout=timeout, port=self.port, check=check, registration=registration,
        )
        if segment is None:
            raise cli.ViewError("The remote catalog did not identify the recording.")
        return url, segment

    def register(self, path: str, check: Callable[[], None]) -> tuple[str, str]:
        payload = (json.dumps(path, ensure_ascii=False) + "\n").encode()
        if len(payload) > cli.MAX_REQUEST_BYTES:
            raise cli.RegistrationError("The remote path is too long.")
        _bootstrap.transfer(self.process, payload, timeout=5)
        return self.read(150, check, registration=True)

    def close(self) -> None:
        try:
            cli.retire_ssh(self.process)
        finally:
            self.process.stdout.close()


def start_catalog(host: str, path: str, resources: dict, requested_port: int | None,
                  check: Callable[[], None], *, interactive: bool = False) -> tuple[Catalog, tuple[str, str]]:
    for _ in range(4):
        port = requested_port if requested_port is not None else cli.control_port()
        try:
            cli.port_available(port)
            command = cli.remote_command(host, path, resources, port, open_session=True)
            if not interactive:
                command[1:1] = ["-o", "BatchMode=yes"]
            print(f"rerun-remote: preparing catalog on {host}…", file=sys.stderr, flush=True)
            with tempfile.TemporaryFile() as diagnostics:
                process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=diagnostics, start_new_session=True,
                )
                catalog = Catalog(process, port)
                try:
                    _bootstrap.transfer(process, _bootstrap.source_payload())
                    return catalog, catalog.read(_bootstrap.SETUP_SECONDS + 200, check)
                except BaseException as error:
                    catalog.close()
                    if not isinstance(error, (cli.ViewError, _bootstrap.SetupError, OSError)):
                        raise
                    size = diagnostics.seek(0, os.SEEK_END)
                    diagnostics.seek(max(0, size - 4096))
                    detail = diagnostics.read(4096).decode(errors="replace").strip()
                    if f"port {port} is occupied" in detail or "Address already in use" in detail:
                        raise cli.PortOccupied(f"Loopback port {port} is occupied.") from error
                    if detail:
                        raise cli.ViewError(f"{error} {detail}") from error
                    raise cli.ViewError(str(error)) from error
        except cli.PortOccupied:
            if requested_port is not None:
                raise
    raise cli.ViewError(f"Could not allocate a free catalog port for {host}.")


def open_recording(desktop: subprocess.Popen, control: int, url: str, segment: str,
                   check: Callable[[], None]) -> None:
    from rerun.experimental import ViewerClient

    def owned():
        check()
        if not cli.viewer_listener_owned(desktop.pid, control):
            raise cli.ViewError("The native Viewer control listener is no longer owned by this session.")

    owned()
    client = ViewerClient.connect(f"rerun+http://127.0.0.1:{control}/proxy")
    before = client.get_state()
    if before.viewer_version != cli.VERSION:
        raise cli.ViewError(f"The native Viewer reported {before.viewer_version}; expected {cli.VERSION}.")
    prior = {recording.store_id for recording in before.recordings}
    result = client.open(url)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        owned()
        state = client.get_state()
        candidates = [
            recording for recording in state.recordings
            if cli.recording_id(recording.store_id) == segment
            and (recording.store_id in result.store_ids or recording.location == url)
        ]
        if not state.loading and len(candidates) == 1:
            if candidates[0].store_id not in prior:
                cli.set_saved_panels(client, candidates[0])
            return
        time.sleep(0.15)
    raise cli.ViewError("The Viewer did not finish opening the selected recording.")


def view(binary: Path, first_host: str, first_path: str, resources: dict,
         requested: tuple[str, int] | None, *, port: int | None = None) -> int:
    catalogs: dict[str, Catalog] = {}
    desktop = None

    def check():
        if desktop is not None and desktop.poll() is not None:
            raise cli.ViewError("The native Viewer closed.")

    def remove(host):
        catalogs.pop(host).close()

    def open_path(target):
        host, path, live = cli.resolve_target(target)
        if host is None or live:
            raise cli.RegistrationError("Enter SSH_ALIAS:/absolute/path/file.rrd.")
        catalog = catalogs.get(host)
        if catalog is not None and catalog.process.poll() is not None:
            remove(host)
            catalog = None
        if catalog is None:
            catalogs[host], recording = start_catalog(host, path, resources, None, check)
        else:
            try:
                recording = catalog.register(path, check)
            except cli.RegistrationError:
                raise
            except BaseException:
                remove(host)
                raise
        open_recording(desktop, control, *recording, check)

    # macOS Unix socket paths have a smaller length limit than normal file paths.
    with tempfile.TemporaryDirectory(prefix="rerun-remote-", dir="/tmp") as temporary, socket.socket(socket.AF_UNIX) as listener:
        endpoint = str(Path(temporary) / "open")
        listener.bind(endpoint)
        listener.listen(1)
        listener.settimeout(0.2)
        try:
            catalogs[first_host], (url, segment) = start_catalog(
                first_host, first_path, resources, port, check, interactive=True,
            )
            control = cli.control_port()
            environment = dict(os.environ, **{REQUEST_SOCKET: endpoint})
            desktop = cli.viewer(binary, url, resources["cpu_count"], control_port=control, env=environment)
            cli.initialize_time(desktop, control, requested, selected_segment=segment, ssh=catalogs[first_host].process, saved_panels=True)
            while desktop.poll() is None:
                for host, catalog in list(catalogs.items()):
                    if catalog.process.poll() is not None:
                        print(f"rerun-remote: SSH connection to {host} ended.", file=sys.stderr, flush=True)
                        remove(host)
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                with connection, connection.makefile("rb") as request:
                    connection.settimeout(5)
                    try:
                        line = request.readline(cli.MAX_REQUEST_BYTES + 1)
                        if not line.endswith(b"\n") or len(line) > cli.MAX_REQUEST_BYTES:
                            raise cli.RegistrationError("Invalid or oversized remote path.")
                        open_path(line[:-1].decode())
                        response = b"OK\n"
                    except Exception as error:
                        message = " ".join(str(error).splitlines()) or "Could not open the remote recording."
                        excerpt = message.encode(errors="replace")[:3500].decode(errors="ignore")
                        response = f"ERROR {excerpt}\n".encode()
                    with suppress(OSError):
                        connection.sendall(response)
            if desktop.returncode:
                raise cli.ViewError(f"Native Viewer exited with status {desktop.returncode}.")
            return 0
        finally:
            with ExitStack() as clean:
                for catalog in catalogs.values():
                    clean.callback(catalog.close)
                if desktop is not None:
                    clean.callback(cli.stop_process, desktop)
