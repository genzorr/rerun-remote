"""Exercise an installed package with a native viewer and isolated local SSH."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import pwd
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


def producer(port: int) -> None:
    import rerun as rr

    recording = rr.RecordingStream("rerun_remote_native_test", recording_id="11111111-2222-4333-8444-555555555555")
    rr.set_sinks(rr.GrpcServerSink(bind_ip="127.0.0.1", port=port), recording=recording)
    tick = 0
    while True:
        recording.set_time("elapsed", duration=tick / 10)
        recording.log("samples", rr.Scalars(tick))
        tick += 1
        time.sleep(0.1)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@unittest.skipUnless(os.environ.get("RERUN_REMOTE_NATIVE_TESTS") == "1", "Set RERUN_REMOTE_NATIVE_TESTS=1 for installed-package native checks")
class TestNative(unittest.TestCase):
    def setUp(self) -> None:
        import psutil
        from rerun.experimental import ViewerClient

        self.psutil = psutil
        self.ViewerClient = ViewerClient
        self.directory = tempfile.TemporaryDirectory(prefix="rerun-remote-native-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.processes = []
        self.addCleanup(self.stop_all)
        self.environment = dict(os.environ, RERUN_ANALYTICS_ENABLED="false", WGPU_BACKEND="vulkan")
        self.environment.pop("PYTHONPATH", None)
        lavapipe = Path("/usr/share/vulkan/icd.d/lvp_icd.json")
        if lavapipe.exists():
            self.environment["VK_ICD_FILENAMES"] = str(lavapipe)
        for kind in ("CONFIG", "DATA", "CACHE", "STATE", "RUNTIME"):
            directory = self.root / kind.lower()
            directory.mkdir(mode=0o700)
            self.environment[f"XDG_{kind}_HOME" if kind != "RUNTIME" else "XDG_RUNTIME_DIR"] = str(directory)
        self.saved = self.root / "recording with spaces; literal.rrd"
        self.save_recording(self.saved)

    def start(self, command: list[str], *, environment: dict | None = None):
        output = (self.root / f"process-{len(self.processes)}.log").open("w+")
        self.addCleanup(output.close)
        process = subprocess.Popen(command, env=environment or self.environment, cwd=self.root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append((process, output))
        return process

    def diagnostics(self) -> str:
        result = []
        for process, output in self.processes:
            output.flush()
            output.seek(0)
            result.append(f"pid={process.pid}, status={process.poll()}: {output.read()[-4000:]}")
        return "\n".join(result)

    def stop_all(self) -> None:
        for process, _ in reversed(self.processes):
            if process.poll() is not None:
                continue
            with contextlib.suppress(self.psutil.Error):
                descendants = self.psutil.Process(process.pid).children(recursive=True)
                for child in reversed(descendants):
                    with contextlib.suppress(self.psutil.Error):
                        child.terminate()
                _, alive = self.psutil.wait_procs(descendants, timeout=5)
                for child in alive:
                    with contextlib.suppress(self.psutil.Error):
                        child.kill()
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)

    def save_recording(self, path: Path, *, blueprint: bool = True) -> None:
        import rerun as rr
        import rerun.blueprint as rrb

        recording = rr.RecordingStream("saved_native_test", recording_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        layout = rrb.Blueprint(rrb.TimeSeriesView(name="Saved samples", contents=["samples"]), rrb.BlueprintPanel(state="Hidden"), rrb.SelectionPanel(state="Hidden"), rrb.TimePanel(state="Hidden")) if blueprint else None
        recording.save(path, default_blueprint=layout)
        for tick in range(3):
            recording.set_time("elapsed", duration=tick)
            recording.log("samples", rr.Scalars(tick))
        recording.disconnect()

    def cli(self, *arguments: str):
        command = [sys.executable, "-m", "rerun_remote", *arguments]
        if sys.platform == "linux":
            command = ["xvfb-run", "-a", "--server-args=-screen 0 1280x720x24 -nolisten tcp -ac -extension GLX", *command]
        return self.start(command)

    def interrupt_cli(self, process) -> None:
        if sys.platform == "linux":
            for child in self.psutil.Process(process.pid).children(recursive=True):
                with contextlib.suppress(self.psutil.Error):
                    if child.cmdline()[1:3] == ["-m", "rerun_remote"]:
                        child.send_signal(signal.SIGINT)
                        return
            self.fail("CLI process was not found under xvfb-run")
        process.send_signal(signal.SIGINT)

    def viewer(self, process, *, timeout: float = 45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("Viewing exited before native readiness.\n" + self.diagnostics())
            with contextlib.suppress(self.psutil.Error):
                for child in self.psutil.Process(process.pid).children(recursive=True):
                    if child.name().lower() not in ("rerun", "rerun.exe"):
                        continue
                    with contextlib.suppress(self.psutil.Error):
                        for connection in child.net_connections(kind="tcp"):
                            if connection.status != self.psutil.CONN_LISTEN or connection.laddr.ip != "127.0.0.1":
                                continue
                            try:
                                client = self.ViewerClient.connect(f"rerun+http://127.0.0.1:{connection.laddr.port}/proxy")
                                state = client.viewer_state()
                                if state.recordings and state.recordings[0].timelines:
                                    return child, client
                            except Exception:
                                pass
            time.sleep(0.2)
        self.fail("Native viewer never loaded recording data.\n" + self.diagnostics())

    def ssh(self, *, attach_only: bool, multiplex: bool = False) -> str:
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        ssh = shutil.which("ssh")
        self.assertIsNotNone(ssh, "OpenSSH client is required")
        for name in ("host", "client"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.root / name)], check=True, capture_output=True)
        port = free_port()
        config = self.root / "sshd_config"
        settings = [f"Port {port}", "ListenAddress 127.0.0.1", f"HostKey {self.root / 'host'}", f"PidFile {self.root / 'sshd.pid'}", f"AuthorizedKeysFile {self.root / 'client.pub'}", "StrictModes no", "PasswordAuthentication no", "KbdInteractiveAuthentication no", "UsePAM no", "AllowTcpForwarding local", "LogLevel ERROR"]
        if attach_only:
            settings.append("ForceCommand /bin/false")
        else:
            settings.append(f"SetEnv PATH={Path(sys.executable).parent}:/usr/bin:/bin XDG_RUNTIME_DIR=/run/user/{os.getuid()} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{os.getuid()}/bus")
        config.write_text("\n".join(settings) + "\n")
        daemon = self.start([sshd, "-D", "-e", "-f", str(config)])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if daemon.poll() is not None:
                self.fail("Isolated sshd failed.\n" + self.diagnostics())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("Isolated sshd did not start")
        client_config = self.root / "ssh_config"
        client_config.write_text(f"Host native-test\n HostName 127.0.0.1\n Port {port}\n User {pwd.getpwuid(os.getuid()).pw_name}\n IdentityFile {self.root / 'client'}\n IdentitiesOnly yes\n BatchMode yes\n StrictHostKeyChecking no\n UserKnownHostsFile {self.root / 'known_hosts'}\n")
        self.master = None
        if multiplex:
            with client_config.open("a") as output:
                output.write(f" ControlMaster auto\n ControlPath {self.root / 'master'}\n ControlPersist 60\n")
            self.master = self.start([ssh, "-F", str(client_config), "-o", "ControlPersist=no", "-MN", "native-test"])
            deadline = time.monotonic() + 10
            while not (self.root / "master").exists() and time.monotonic() < deadline:
                self.assertIsNone(self.master.poll(), self.diagnostics())
                time.sleep(0.1)
            self.assertTrue((self.root / "master").exists(), self.diagnostics())
        self.ssh_binary = ssh
        self.ssh_config = client_config
        commands = self.root / "bin"
        commands.mkdir()
        wrapper = commands / "ssh"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.join([ssh, "-F", str(client_config)]) + ' "$@"\n')
        wrapper.chmod(0o755)
        self.environment["PATH"] = str(commands) + os.pathsep + os.environ["PATH"]
        return "native-test"

    def assert_advancing(self, client) -> None:
        def end():
            state = client.viewer_state()
            recording = state.recordings[0]
            self.assertIn("11111111-2222-4333-8444-555555555555", recording.store_id)
            return next(timeline.end for timeline in recording.timelines if timeline.name == "elapsed")
        initial = end()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if end() > initial:
                return
            time.sleep(0.2)
        self.fail("Live samples stopped advancing")

    def assert_saved_state(self, client, name: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = client.viewer_state()
            if state.recordings[0].current_time == 1_234_567_890:
                break
            time.sleep(0.2)
        self.assertEqual(state.recordings[0].current_timeline, "elapsed")
        self.assertEqual(state.recordings[0].current_time, 1_234_567_890)
        self.assertIn("Saved samples", [view.name for view in state.views])
        time.sleep(0.5)
        self.assertEqual(client.viewer_state().recordings[0].current_time, 1_234_567_890, "Startup cursor must remain paused")
        artifacts = Path(os.environ.get("RERUN_REMOTE_NATIVE_ARTIFACTS", str(self.root)))
        artifacts.mkdir(parents=True, exist_ok=True)
        client.save_screenshot(str(artifacts / f"{name}.png"))

    def test_local_saved_blueprint_and_exact_time(self) -> None:
        process = self.cli(str(self.saved), "--time", "elapsed=1234567890")
        _, client = self.viewer(process)
        self.assert_saved_state(client, "local-saved-blueprint")
        self.interrupt_cli(process)
        self.assertEqual(process.wait(timeout=20), 130, self.diagnostics())

    def test_local_saved_without_blueprint(self) -> None:
        path = self.root / "without-blueprint.rrd"
        self.save_recording(path, blueprint=False)
        process = self.cli(str(path))
        _, client = self.viewer(process)
        self.assertIn("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", client.viewer_state().recordings[0].store_id)

    def test_live_producer_survives_viewer_exit_and_interrupt(self) -> None:
        self.live_attachment(remote=False)

    def test_ssh_live_producer_survives_viewer_exit_and_interrupt(self) -> None:
        self.live_attachment(remote=True)

    def live_attachment(self, *, remote: bool) -> None:
        port = free_port()
        source = self.start([sys.executable, str(Path(__file__).resolve()), "_producer", str(port)])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.assertIsNone(source.poll(), self.diagnostics())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("Synthetic producer did not start.\n" + self.diagnostics())
        url = f"rerun+http://127.0.0.1:{port}/proxy"
        arguments = [url]
        if remote:
            alias = self.ssh(attach_only=True, multiplex=True)
            forwarding_port = free_port()
            arguments = ["--port", str(forwarding_port), f"{alias}:{url}"]
        for closure in (("viewer", "interrupt", "ssh-loss") if remote else ("viewer", "interrupt")):
            process = self.cli(*arguments)
            desktop, client = self.viewer(process)
            self.assert_advancing(client)
            if closure == "viewer":
                desktop.terminate()
            elif closure == "interrupt":
                self.interrupt_cli(process)
            else:
                tunnels = [child for child in self.psutil.Process(process.pid).children(recursive=True) if child.name() == "ssh"]
                self.assertEqual(len(tunnels), 1)
                tunnels[0].terminate()
            process.wait(timeout=25)
            desktop.wait(timeout=5)
            self.assertIsNone(source.poll(), self.diagnostics())
            if remote:
                self.assertIsNone(self.master.poll(), "Existing SSH master must survive")
                subprocess.run([self.ssh_binary, "-F", str(self.ssh_config), "-O", "check", "native-test"], check=True, capture_output=True, timeout=5)
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", forwarding_port), timeout=0.2)
        failed = self.cli(*arguments, "--time", "elapsed=-1")
        self.assertEqual(failed.wait(timeout=25), 1, self.diagnostics())
        self.assertIsNone(source.poll())

    def test_ssh_file_server_with_effective_resource_limits_and_native_catalog_view(self) -> None:
        alias = self.ssh(attach_only=False)
        port = free_port()
        server = [str(Path(sys.executable).with_name("rerun-remote")), "_serve", "--recording", str(self.saved), "--port", str(port), "--cpu-count", "1", "--runtime-seconds", "120"]
        output = (self.root / "server-stderr.log").open("w+")
        self.addCleanup(output.close)
        process = subprocess.Popen([self.ssh_binary, "-F", str(self.ssh_config), "-S", "none", alias, shlex.join(server)], env=self.environment, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=output, start_new_session=True)
        self.processes.append((process, output))
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stdin.close)
        self.assertTrue(select.select([process.stdout], [], [], 45)[0], "File server readiness timed out")
        response = process.stdout.readline()
        self.assertTrue(response, self.diagnostics())
        ready = json.loads(response)
        self.assertEqual(ready["version"], "0.40.0")
        self.assertIn(f"127.0.0.1:{port}/", ready["segment_url"])
        import rerun_cli
        import numpy as np
        binary = Path(rerun_cli.__file__).with_name("rerun")
        desktop = self.start(["xvfb-run", "-a", "--server-args=-screen 0 1280x720x24 -nolisten tcp -ac -extension GLX", str(binary), "--bind", "127.0.0.1", "--port", "auto", ready["segment_url"]])
        _, client = self.viewer(desktop)
        client.set_time("elapsed", duration=np.timedelta64(1_234_567_890, "ns"), play=False)
        self.assert_saved_state(client, "ssh-server-blueprint")
        process.stdin.close()
        self.assertEqual(process.wait(timeout=25), 0, self.diagnostics())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)

    @unittest.skipUnless(os.environ.get("RERUN_REMOTE_TEST_HOST"), "Set RERUN_REMOTE_TEST_HOST and RERUN_REMOTE_TEST_DIRECTORY for a disposable cross-host check")
    def test_cross_host_saved_file_with_custom_port_and_exact_time(self) -> None:
        host, remote_file, cache_home = self.remote_recording()
        self.remote_environment({"XDG_CACHE_HOME": cache_home})
        for offline in (False, True):
            if offline:
                self.remote_environment({"XDG_CACHE_HOME": cache_home, "UV_OFFLINE": "1", "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"})
            process = self.cli("--port", str(free_port()), "--cpu-count", "1", "--runtime-seconds", "600", f"{host}:{remote_file}", "--time", "elapsed=1234567890")
            _, client = self.viewer(process, timeout=600)
            self.assert_saved_state(client, "cross-host-warm-blueprint" if offline else "cross-host-cold-blueprint")
            self.interrupt_cli(process)
            self.assertEqual(process.wait(timeout=25), 130, self.diagnostics())

    def remote_environment(self, settings: dict[str, str]) -> None:
        ssh = shutil.which("ssh")
        commands = self.root / "remote-bin"
        commands.mkdir(exist_ok=True)
        wrapper = commands / "ssh"
        prefix = "export " + " ".join(key + "=" + shlex.quote(value) for key, value in settings.items()) + "; "
        wrapper.write_text("#!" + sys.executable + "\nimport os,sys\narguments=sys.argv[1:]\nif '-N' not in arguments:\n arguments[-1]=" + repr(prefix) + "+arguments[-1]\nos.execv(" + repr(ssh) + ", [" + repr(ssh) + ", *arguments])\n")
        wrapper.chmod(0o755)
        self.environment["PATH"] = str(commands) + os.pathsep + os.environ["PATH"]

    def remote_python(self, source: str, *arguments: str) -> str:
        host = os.environ["RERUN_REMOTE_TEST_HOST"]
        command = shlex.join(["python3", "-I", "-c", source, *arguments])
        return subprocess.run([shutil.which("ssh"), "-S", "none", "-T", "-o", "ConnectTimeout=10", host, command], check=True, capture_output=True, text=True, timeout=30).stdout

    def remote_recording(self) -> tuple[str, str, str]:
        host = os.environ["RERUN_REMOTE_TEST_HOST"]
        parent = os.environ["RERUN_REMOTE_TEST_DIRECTORY"].rstrip("/")
        directory = parent + "/" + self.root.name
        self.addCleanup(self.clean_remote_directory, parent, directory, self.root.name)
        source = "\n".join((
            "import os,sys",
            "from pathlib import Path",
            "parent=Path(sys.argv[1]);root=Path(sys.argv[2])",
            "assert parent.is_absolute() and root.parent==parent and root.name==sys.argv[3] and root.name.startswith('rerun-remote-native-')",
            "assert parent.stat().st_uid==os.getuid()",
            "root.mkdir(mode=0o700)",
            "(root/'.test-owner').write_text(sys.argv[3])",
        ))
        self.remote_python(source, parent, directory, self.root.name)
        path = directory + "/" + self.saved.name
        subprocess.run(["scp", "-o", "ControlPath=none", str(self.saved), f"{host}:{path}"], check=True, capture_output=True, timeout=30)
        return host, path, directory + "/cache"

    def clean_remote_directory(self, parent: str, directory: str, token: str) -> None:
        self.stop_all()
        source = "\n".join((
            "import os,shutil,sys,time",
            "from pathlib import Path",
            "parent=Path(sys.argv[1]);root=Path(sys.argv[2])",
            "if not root.exists():sys.exit(0)",
            "assert root.parent==parent and root.name==sys.argv[3] and root.name.startswith('rerun-remote-native-')",
            "assert not root.is_symlink() and root.stat().st_uid==os.getuid() and (root/'.test-owner').read_text()==sys.argv[3]",
            "prefix=os.fsencode(str(root)+'/');deadline=time.monotonic()+10",
            "while True:",
            " active=[]",
            " for entry in Path('/proc').iterdir():",
            "  if not entry.name.isdigit() or int(entry.name)==os.getpid():continue",
            "  try:arguments=(entry/'cmdline').read_bytes().split(b'\\0')",
            "  except OSError:continue",
            "  if any(argument.startswith(prefix) for argument in arguments):active.append(entry.name)",
            " if not active:break",
            " if time.monotonic()>=deadline:raise RuntimeError('Owned remote test processes remain: '+repr(active))",
            " time.sleep(0.2)",
            "shutil.rmtree(root)",
            "assert not root.exists() and parent.is_dir()",
        ))
        self.remote_python(source, parent, directory, token)

    def remote_profiles(self, cache_home: str) -> dict:
        source = "import json,sys; from pathlib import Path; root=Path(sys.argv[1])/'rerun-remote'/'profiles'; print(json.dumps({path.parent.name:path.stat().st_mtime_ns for path in root.glob('*/complete.json')}))"
        return json.loads(self.remote_python(source, cache_home))

    @unittest.skipUnless(os.environ.get("RERUN_REMOTE_TEST_HOST"), "Requires an authorized disposable cross-host setup")
    def test_cross_host_concurrent_setup_and_changed_source_preserve_active_profile(self) -> None:
        import rerun_remote

        host, path, cache_home = self.remote_recording()
        self.remote_environment({"XDG_CACHE_HOME": cache_home})
        arguments = ["--cpu-count", "1", "--runtime-seconds", "600", f"{host}:{path}", "--time", "elapsed=1234567890"]
        viewers = [self.cli("--port", str(free_port()), *arguments) for _ in range(2)]
        clients = []
        for process in viewers:
            _, client = self.viewer(process, timeout=600)
            self.assert_saved_state(client, "concurrent-saved-blueprint")
            clients.append(client)
        original = self.remote_profiles(cache_home)
        self.assertEqual(len(original), 1, "Concurrent first uses must share one complete profile")
        variant = self.root / "variant"
        shutil.copytree(Path(rerun_remote.__file__).parent, variant / "rerun_remote", ignore=shutil.ignore_patterns("__pycache__"))
        with (variant / "rerun_remote/cli.py").open("a") as output:
            output.write("\n# Distinct valid source build for the cache identity check.\n")
        self.environment["PYTHONPATH"] = str(variant)
        changed = self.cli("--port", str(free_port()), *arguments)
        _, client = self.viewer(changed, timeout=600)
        self.assert_saved_state(client, "changed-source-blueprint")
        profiles = self.remote_profiles(cache_home)
        self.assertEqual(len(profiles), 2, "Different source with the same package version needs its own profile")
        for identity, modified in original.items():
            self.assertEqual(profiles[identity], modified, "A completed active profile must remain unchanged")
        self.assertEqual(clients[0].viewer_state().recordings[0].current_time, 1_234_567_890)
        for process in [*viewers, changed]:
            self.interrupt_cli(process)
            self.assertEqual(process.wait(timeout=25), 130, self.diagnostics())

    @unittest.skipUnless(os.environ.get("RERUN_REMOTE_TEST_HOST"), "Requires an authorized disposable cross-host setup")
    def test_cross_host_interrupted_setup_can_retry_without_partial_profile(self) -> None:
        host, path, cache_home = self.remote_recording()
        marker = cache_home + "-install-started"
        commands = cache_home + "-bin"
        source = "\n".join((
            "import os,shlex,shutil,sys",
            "from pathlib import Path",
            "root=Path(sys.argv[1]);root.mkdir(mode=0o700)",
            "uv=shutil.which('uv') or str(Path.home()/'.local/bin/uv')",
            'script = "\\n".join(("#!/usr/bin/python3", "import os,sys,time", "from pathlib import Path", "if \'--version\' not in sys.argv[1:]:", "    Path(" + repr(sys.argv[2]) + ").touch()", "    time.sleep(120)", "os.execv(" + repr(uv) + ", [" + repr(uv) + ", *sys.argv[1:]])", ""))',
            "path=root/'uv';path.write_text(script);path.chmod(0o700)",
        ))
        self.remote_python(source, commands, marker)
        self.remote_environment({"XDG_CACHE_HOME": cache_home, "PATH": commands + ":/usr/bin:/bin"})
        arguments = ["--cpu-count", "1", "--runtime-seconds", "600", f"{host}:{path}", "--time", "elapsed=1234567890"]
        interrupted = self.cli("--port", str(free_port()), *arguments)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.assertIsNone(interrupted.poll(), self.diagnostics())
            if self.remote_python("import sys;from pathlib import Path;print(Path(sys.argv[1]).exists())", marker).strip() == "True":
                break
            time.sleep(0.5)
        else:
            self.fail("Injected slow installer did not start.\n" + self.diagnostics())
        self.interrupt_cli(interrupted)
        self.assertEqual(interrupted.wait(timeout=25), 130, self.diagnostics())
        self.assertEqual(self.remote_profiles(cache_home), {}, "Interrupted installation must not publish a profile")
        self.remote_environment({"XDG_CACHE_HOME": cache_home})
        retried = self.cli("--port", str(free_port()), *arguments)
        _, client = self.viewer(retried, timeout=600)
        self.assert_saved_state(client, "retried-setup-blueprint")
        self.assertEqual(len(self.remote_profiles(cache_home)), 1)
        self.interrupt_cli(retried)
        self.assertEqual(retried.wait(timeout=25), 130, self.diagnostics())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "_producer":
        producer(int(sys.argv[2]))
    else:
        unittest.main()
