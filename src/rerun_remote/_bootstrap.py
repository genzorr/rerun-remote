"""Bounded, standard-library-only transport and private remote runtime setup."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

SCHEMA = 1
PYTHON = "3.12"
REQUIREMENTS = ["rerun-sdk==0.40.0", "datafusion==54.0.0"]
MAX_PAYLOAD = 2 * 1024 * 1024
MAX_HEADER = 4096
SETUP_SECONDS = 900
TRANSFER_SECONDS = 30
UV_VERSION = "0.12.3"
UV_HASHES = {
    "x86_64": "600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101",
    "aarch64": "bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2",
}


class SetupError(RuntimeError):
    """A bounded remote setup failure."""


def profile_manifest(source: bytes) -> dict:
    return {"schema": SCHEMA, "python": PYTHON, "requirements": REQUIREMENTS, "source_sha256": hashlib.sha256(source).hexdigest()}


def profile_identity(manifest: dict) -> str:
    profile = dict(manifest, platform=sys.platform, architecture=os.uname().machine)
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_payload() -> bytes:
    package = Path(__file__).parent
    license_path = package / "LICENSE"
    if not license_path.is_file():
        license_path = package.parent.parent / "LICENSE"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in [*sorted(package.glob("*.py")), license_path]:
            archive.writestr(zipfile.ZipInfo("rerun_remote/" + path.name, (2020, 1, 1, 0, 0, 0)), path.read_bytes())
    source = output.getvalue()
    validate_source(source)
    header = json.dumps(dict(profile_manifest(source), size=len(source)), sort_keys=True).encode()
    return struct.pack("!I", len(header)) + header + source


def validate_source(source: bytes) -> None:
    if not 0 < len(source) <= MAX_PAYLOAD:
        raise SetupError("Source payload size exceeds the supported bound.")
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > 32 or len(set(names)) != len(names) or not {"rerun_remote/__init__.py", "rerun_remote/__main__.py", "rerun_remote/cli.py", "rerun_remote/_bootstrap.py", "rerun_remote/LICENSE"}.issubset(names):
                raise SetupError("Invalid source package entries.")
            if sum(entry.file_size for entry in entries) > MAX_PAYLOAD:
                raise SetupError("Expanded source payload exceeds the supported bound.")
            for entry in entries:
                if (entry.filename != "rerun_remote/LICENSE" and not re.fullmatch(r"rerun_remote/[a-z_][a-z_0-9]*\.py", entry.filename)) or entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1 or (entry.external_attr >> 16) & 0o170000 not in (0, 0o100000):
                    raise SetupError("Unsafe source package path or entry.")
                archive.read(entry)
    except (zipfile.BadZipFile, RuntimeError, EOFError) as error:
        raise SetupError("Invalid source ZIP payload.") from error


def read_payload(descriptor: int, *, timeout: float = TRANSFER_SECONDS) -> tuple[dict, bytes]:
    deadline = time.monotonic() + timeout
    def exact(size):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SetupError("Source payload transfer timed out.")
            if not select.select([descriptor], [], [], min(0.2, remaining))[0]:
                continue
            chunk = os.read(descriptor, min(65536, size - len(data)))
            if not chunk:
                raise SetupError("SSH connection closed during source transfer.")
            data.extend(chunk)
        return bytes(data)
    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= MAX_HEADER:
        raise SetupError("Invalid source payload header size.")
    try:
        header = json.loads(exact(size))
        length = header.pop("size")
        if type(length) is not int or not 0 < length <= MAX_PAYLOAD:
            raise ValueError("invalid payload size")
        source = exact(length)
        if header != profile_manifest(source):
            raise ValueError("source hash or runtime profile mismatch")
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        raise SetupError("Invalid source payload identity or requirements.") from error
    validate_source(source)
    return header, source


def transfer(process: subprocess.Popen, payload: bytes, *, timeout: float = TRANSFER_SECONDS) -> None:
    descriptor = process.stdin.fileno()
    os.set_blocking(descriptor, False)
    deadline = time.monotonic() + timeout
    try:
        offset = 0
        while offset < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SetupError("Source payload transfer timed out.")
            if process.poll() is not None:
                raise SetupError("SSH closed during remote setup; check the remote diagnostic.")
            if not select.select([], [descriptor], [], min(0.2, remaining))[1]:
                continue
            try:
                offset += os.write(descriptor, payload[offset:offset + 65536])
            except BlockingIOError:
                continue
    except BrokenPipeError as error:
        raise SetupError("SSH closed during remote setup; check the remote diagnostic.") from error
    finally:
        os.set_blocking(descriptor, True)


class Lifecycle:
    def __init__(self, descriptor: int | None = None, *, timeout: float = SETUP_SECONDS):
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.deadline = time.monotonic() + timeout
        self.watcher = None
        if descriptor is not None:
            def watch():
                while not self.finished.is_set():
                    if select.select([descriptor], [], [], 0.2)[0]:
                        os.read(descriptor, 4096)
                        self.cancelled.set()
                        return
            self.watcher = threading.Thread(target=watch, daemon=True)
            self.watcher.start()

    def check(self):
        if self.cancelled.is_set():
            raise SetupError("SSH connection closed during remote setup.")
        if time.monotonic() >= self.deadline:
            raise SetupError("Remote setup exceeded its 900-second deadline; retry with working HTTPS package access.")

    def close(self):
        self.finished.set()
        if self.watcher is not None:
            self.watcher.join(timeout=1)

    def run(self, command: list[str], *, env: dict | None = None):
        self.check()
        with tempfile.TemporaryFile() as diagnostic:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=diagnostic, stderr=diagnostic, env=env, start_new_session=True)
            try:
                while child.poll() is None:
                    self.check()
                    self.cancelled.wait(0.1)
                if child.returncode:
                    diagnostic.seek(0, os.SEEK_END)
                    diagnostic.seek(max(0, diagnostic.tell() - 4096))
                    raise SetupError("Runtime preparation failed: " + diagnostic.read(4096).decode(errors="replace").strip())
            finally:
                # A completed installer can leave descendants; retire its entire owned group.
                from rerun_remote.cli import stop_process
                stop_process(child, grace=1)


def private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise SetupError("Remote cache must be a private directory owned by this user.")
    path.chmod(0o700)
    return path


def cache_root() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    if not base.is_absolute():
        raise SetupError("XDG_CACHE_HOME must be an absolute path.")
    return private_directory(base / "rerun-remote")


@contextlib.contextmanager
def profile_lock(root: Path, identity: str, lifecycle: Lifecycle):
    import fcntl
    directory = private_directory(root / "locks")
    descriptor = os.open(directory / (identity + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            lifecycle.check()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                lifecycle.cancelled.wait(0.1)
        yield
    finally:
        os.close(descriptor)


def download_uv(url: str, destination: Path, lifecycle: Lifecycle) -> None:
    # The downloader is a child so cancellation also interrupts DNS, TLS and blocked reads.
    code = "\n".join((
        "import sys,urllib.request",
        "with urllib.request.urlopen(sys.argv[1],timeout=20) as response, open(sys.argv[2],'xb') as output:",
        " if not response.url.startswith('https://'): raise RuntimeError('HTTPS redirect required')",
        " size=0",
        " while True:",
        "  chunk=response.read(65536)",
        "  if not chunk: break",
        "  size+=len(chunk)",
        "  if size>80*1024*1024: raise RuntimeError('uv archive too large')",
        "  output.write(chunk)",
    ))
    lifecycle.run([sys.executable, "-I", "-c", code, url, str(destination)])


def find_uv() -> str | None:
    found = shutil.which("uv")
    standard = Path.home() / ".local/bin/uv"
    return found or (str(standard) if standard.is_file() and os.access(standard, os.X_OK) else None)


def uv_executable(root: Path, lifecycle: Lifecycle) -> str:
    existing = find_uv()
    if existing:
        return existing
    arch = os.uname().machine
    if arch not in UV_HASHES:
        raise SetupError("Automatic uv download supports x86_64/aarch64 Linux; install uv in PATH for this architecture.")
    tools = private_directory(root / "tools")
    target = tools / ("uv-" + UV_VERSION + "-" + arch)
    if target.is_file() and not target.is_symlink() and os.access(target, os.X_OK):
        return str(target)
    artifact = "uv-" + arch + "-unknown-linux-gnu"
    with tempfile.TemporaryDirectory(prefix="uv-", dir=tools) as temporary:
        archive = Path(temporary) / "uv.tar.gz"
        download_uv(f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{artifact}.tar.gz", archive, lifecycle)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != UV_HASHES[arch]:
            raise SetupError("Official uv archive checksum mismatch; nothing was installed.")
        with tarfile.open(archive, "r:gz") as bundle:
            member = bundle.getmember(artifact + "/uv")
            if not member.isfile() or member.size > 160 * 1024 * 1024:
                raise SetupError("Invalid uv executable archive entry.")
            executable = Path(temporary) / "uv"
            with bundle.extractfile(member) as source, executable.open("xb") as output:
                shutil.copyfileobj(source, output)
        executable.chmod(0o700)
        lifecycle.check()
        os.replace(executable, target)
    return str(target)


def runtime_environment(profile: Path) -> dict:
    return dict(os.environ, PYTHONPATH=str(profile / "source.zip"), PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1", UV_NO_CONFIG="1")


def validate_runtime(profile: Path, lifecycle: Lifecycle) -> None:
    code = "import sys,importlib.metadata as m; assert sys.version_info[:2]==(3,12), 'Python 3.12 required'; assert m.version('rerun-sdk')=='0.40.0', 'SDK version mismatch'; assert m.version('datafusion')=='54.0.0', 'DataFusion version mismatch'; from rerun_remote.cli import native_binary; native_binary(server=True)"
    lifecycle.run([str(profile / "env/bin/python"), "-c", code], env=runtime_environment(profile))


def prepare_runtime(root: Path, manifest: dict, source: bytes, lifecycle: Lifecycle) -> Path:
    identity = profile_identity(manifest)
    profiles = private_directory(root / "profiles")
    target = profiles / identity
    with profile_lock(root, identity, lifecycle):
        lifecycle.check()
        if target.exists():
            if target.is_symlink() or (target / "complete.json").read_text() != json.dumps(manifest, sort_keys=True):
                raise SetupError("Cached runtime is incomplete or invalid; preserve it and use a fresh isolated cache.")
            if (target / "source.zip").read_bytes() != source:
                raise SetupError("Cached runtime source identity differs; use a fresh isolated cache.")
            print("rerun-remote: reusing prepared remote runtime", file=sys.stderr, flush=True)
            validate_runtime(target, lifecycle)
            return target
        print("rerun-remote: preparing remote runtime (first use; HTTPS downloads may take several minutes)", file=sys.stderr, flush=True)
        staging = private_directory(root / "staging")
        with tempfile.TemporaryDirectory(prefix=identity + "-", dir=staging) as temporary:
            stage = Path(temporary) / "profile"
            stage.mkdir(mode=0o700)
            (stage / "source.zip").write_bytes(source)
            uv = uv_executable(root, lifecycle)
            environment = dict(runtime_environment(stage), UV_CACHE_DIR=str(root / "uv-cache"), UV_PYTHON_INSTALL_DIR=str(root / "python"), UV_PYTHON_DOWNLOADS="automatic")
            environment.pop("UV_PYTHON_PREFERENCE", None)
            environment.pop("UV_NO_MANAGED_PYTHON", None)
            lifecycle.run([uv, "venv", "--python", PYTHON, "--managed-python", str(stage / "env")], env=environment)
            lifecycle.run([uv, "pip", "install", "--python", str(stage / "env/bin/python"), "--only-binary", ":all:", *REQUIREMENTS], env=environment)
            validate_runtime(stage, lifecycle)
            lifecycle.check()
            (stage / "complete.json").write_text(json.dumps(manifest, sort_keys=True))
            os.rename(stage, target)
            try:
                # Renamed virtualenv script shebangs are stale; only the final Python is used.
                validate_runtime(target, lifecycle)
                lifecycle.check()
            except BaseException:
                shutil.rmtree(target)
                raise
    return target


def main(arguments: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if arguments is None else arguments)
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt)
    try:
        if sys.version_info < (3, 10):
            raise SetupError("Remote setup requires an existing Python 3.10 or later (python3 in PATH).")
        if argv and argv[0] == "--controlled":
            _, unit, source_path, manifest_json, *worker_args = argv
            sys.path.insert(0, source_path)
            from rerun_remote import cli
            args = cli.parser("_worker").parse_args(["--unit", unit, *worker_args])
            resources = {key: getattr(args, key) for key in cli.DEFAULT_RESOURCES}
            lifecycle = Lifecycle(sys.stdin.fileno())
            try:
                cli.verify_controls(unit, resources, os.getpid())
                cli.selected_file(args.recording)
                cli.port_available(args.port)
                manifest = json.loads(manifest_json)
                source = Path(source_path).read_bytes()
                target = prepare_runtime(cache_root(), manifest, source, lifecycle)
                lifecycle.check()
            finally:
                lifecycle.close()
            environment = runtime_environment(target)
            os.execve(str(target / "env/bin/python"), [str(target / "env/bin/python"), "-m", "rerun_remote", "_worker", "--unit", unit, *worker_args], environment)
        manifest, source = read_payload(sys.stdin.fileno())
        with tempfile.TemporaryDirectory(prefix="rerun-remote-source-") as temporary:
            path = Path(temporary) / "source.zip"
            path.write_bytes(source)
            sys.path.insert(0, str(path))
            from rerun_remote import cli
            args = cli.parser("_serve").parse_args(argv)
            unit, command = cli.scope_command(args, [sys.executable, "-I", "-c", zipfile.ZipFile(io.BytesIO(source)).read("rerun_remote/_bootstrap.py").decode(), "--controlled", "UNIT", str(path), json.dumps(manifest), *argv])
            command[command.index("--controlled") + 1] = unit
            process = subprocess.Popen(command, start_new_session=True)
            try:
                return process.wait()
            finally:
                cli.stop_process(process)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"rerun-remote: remote setup failed: {str(error)[:5000]}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
