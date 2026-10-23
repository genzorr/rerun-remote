import contextlib
import io
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from rerun_remote import cli


class TestInputs(unittest.TestCase):
    def test_startup_time_preserves_signed_integer_nanoseconds(self):
        for value in ("arrival=1234567890123456789", "duration=-17", "duration=+17", "duration=9223372036854775807", "duration=-9223372036854775807"):
            name, integer = value.split("=")
            self.assertEqual(cli.startup_time(value), (name, int(integer)))

    def test_startup_time_rejects_invalid_units_and_nat(self):
        import argparse
        for value in ("", "=1", " =1", "arrival=1.2", "arrival=1e9", "arrival=1ns", "arrival=", "arrival=1=2", "arrival=-9223372036854775808", "arrival=9223372036854775808", "arrival=NaT"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                cli.startup_time(value)

    def test_missing_input_fails_before_viewer(self):
        with patch.object(cli, "native_binary") as native:
            with contextlib.redirect_stderr(io.StringIO()) as diagnostic:
                status = cli.main(["/nonexistent-rerun-remote-test.rrd"])
        self.assertEqual(status, 1)
        native.assert_not_called()
        self.assertIn("Cannot open recording", diagnostic.getvalue())

    def test_local_metacharacters_are_a_literal_absolute_argument(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "selected $(touch surprise); 'quoted'.rrd"
            file.touch()
            with patch.object(cli, "native_binary", return_value=Path("/fake/native")), patch.object(cli, "local_view", return_value=0) as view:
                self.assertEqual(cli.main([str(file)]), 0)
            self.assertEqual(view.call_args.args[1], str(file.resolve()))
            self.assertFalse((Path(root) / "surprise").exists())

    def test_native_viewer_receives_file_literally(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            output = root / "arguments.json"
            executable = root / "native viewer"
            executable.write_text('#!' + sys.executable + '\nimport json,sys\nfrom pathlib import Path\nPath(' + repr(str(output)) + ').write_text(json.dumps(sys.argv[1:]))\n')
            executable.chmod(0o755)
            recording = root / "$(touch surprise); 'literal'.rrd"
            recording.touch()
            with patch.object(cli, "initialize_time", side_effect=AssertionError("No-time viewing must not use desktop controls")):
                self.assertEqual(cli.local_view(executable, str(recording)), 0)
            self.assertEqual(json.loads(output.read_text())[-1], str(recording))
            self.assertFalse((root / "surprise").exists())

    def test_remote_paths_must_be_absolute(self):
        with self.assertRaisesRegex(cli.ViewError, "must be absolute"):
            cli.resolve_target("example:recording.rrd")
        self.assertEqual(cli.resolve_target("example:/recordings with spaces/recording.rrd"), ("example", "/recordings with spaces/recording.rrd", False))

    def test_host_cannot_inject_ssh_options(self):
        for alias in ("-oProxyCommand=bad", "a b", "user@host", "host;touch"):
            with self.subTest(alias=alias), self.assertRaises(cli.ViewError):
                cli.host_alias(alias)

    def test_occupied_port_preserves_listener(self):
        with socket.socket() as owner:
            owner.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            owner.bind(("127.0.0.1", 0))
            owner.listen()
            port = owner.getsockname()[1]
            with self.assertRaisesRegex(cli.ViewError, "owner was preserved"):
                cli.port_available(port)
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                connection, _ = owner.accept()
                connection.close()

    def test_closed_session_in_time_wait_does_not_block_repeat(self):
        with socket.socket() as owner:
            owner.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            owner.bind(("127.0.0.1", 0))
            owner.listen()
            port = owner.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
                connection, _ = owner.accept()
                connection.shutdown(socket.SHUT_WR)
                self.assertEqual(client.recv(1), b"")
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(connection.recv(1), b"")
                connection.close()
        with socket.socket() as plain_probe:
            with self.assertRaises(OSError):
                plain_probe.bind(("127.0.0.1", port))
        cli.port_available(port)

    @unittest.skipUnless(sys.platform == "linux", "Listener ownership uses Linux procfs")
    def test_listener_ownership_preserves_unrelated_listener(self):
        with socket.socket() as owner:
            owner.bind(("127.0.0.1", 0))
            owner.listen()
            port = owner.getsockname()[1]
            self.assertTrue(cli.listener_owned(os.getpid(), port))
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            try:
                self.assertFalse(cli.listener_owned(child.pid, port))
                self.assertTrue(cli.listener_owned(os.getpid(), port))
            finally:
                cli.stop_process(child)

    def test_ssh_shell_preserves_recording_and_executable_paths(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            output = root / "args.json"
            directory = root / "remote helper 'quoted'"
            directory.mkdir()
            executable = directory / "rerun-remote"
            executable.write_text('#!' + sys.executable + '\nimport json,sys\nfrom pathlib import Path\nPath(' + repr(str(output)) + ').write_text(json.dumps(sys.argv[1:]))\n')
            executable.chmod(0o755)
            recording = "/recordings/a $(touch " + str(root / "injected") + "); 'quoted'.rrd"
            command = cli.remote_command("example", recording, cli.DEFAULT_RESOURCES, 43210)
            subprocess.run(["sh", "-c", command[-1]], env=dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"]), check=True, timeout=5)
            arguments = json.loads(output.read_text())
            self.assertEqual(arguments[arguments.index("--recording") + 1], recording)
            self.assertEqual(arguments[arguments.index("--port") + 1], "43210")
            self.assertFalse((root / "injected").exists())

    @unittest.skipUnless(shutil.which("ssh"), "Requires the OpenSSH client")
    def test_owned_ssh_connection_preserves_alias_settings_and_disables_multiplexing(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "ssh_config"
            config.write_text(f"Host example\n  HostName 127.0.0.1\n  User fixture\n  Port 22222\n  IdentityFile {root}/identity\n  ProxyJump fixture-jump\n  ControlMaster auto\n  ControlPath {root}/foreign-master\n")
            command = cli.ssh_command("example", 43210, 9876)
            result = subprocess.run(command[:1] + ["-F", str(config), "-G"] + command[1:], capture_output=True, text=True, check=True, timeout=5)
            settings = dict(line.split(" ", 1) for line in result.stdout.splitlines())
            self.assertEqual(settings.get("controlpath", "none"), "none")
            self.assertEqual(settings["hostname"], "127.0.0.1")
            self.assertEqual(settings["user"], "fixture")
            self.assertEqual(settings["port"], "22222")
            self.assertEqual(settings["identityfile"], f"{root}/identity")
            self.assertEqual(settings["proxyjump"], "fixture-jump")

    @unittest.skipUnless(shutil.which("ssh") and hasattr(socket, "AF_UNIX"), "Requires OpenSSH and Unix sockets")
    def test_owned_ssh_never_contacts_preexisting_master(self):
        with tempfile.TemporaryDirectory() as root, socket.socket(socket.AF_UNIX) as master, socket.socket() as reserved:
            master_path = str(Path(root) / "foreign-master")
            master.bind(master_path)
            master.listen()
            master.settimeout(.05)
            reserved.bind(("127.0.0.1", 0))
            upstream_port = reserved.getsockname()[1]
            config = Path(root) / "ssh_config"
            config.write_text(f"Host example\n  HostName 127.0.0.1\n  Port {upstream_port}\n  ControlMaster auto\n  ControlPath {master_path}\n")
            contacted = threading.Event()
            finished = threading.Event()
            def watch_master():
                while not finished.is_set():
                    try:
                        connection, _ = master.accept()
                    except socket.timeout:
                        continue
                    contacted.set()
                    connection.close()
            watcher = threading.Thread(target=watch_master)
            watcher.start()
            try:
                command = cli.ssh_command("example", cli.control_port(), 9876)
                result = subprocess.run(command[:1] + ["-F", str(config)] + command[1:] + ["true"], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 255)
                self.assertFalse(contacted.is_set())
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.connect(master_path)
                    self.assertTrue(contacted.wait(timeout=1))
            finally:
                finished.set()
                watcher.join(timeout=1)

    def test_live_targets_preserve_supported_proxy_urls(self):
        for url in ("rerun+http://127.0.0.1:9876/proxy", "rerun+http://localhost:123/proxy?name=a%20b&x=1", "rerun+http://[::1]:65535/proxy"):
            with self.subTest(url=url):
                self.assertEqual(cli.resolve_target(url), (None, url, True))
                self.assertEqual(cli.resolve_target("example:" + url), ("example", url, True))

    def test_invalid_live_targets_fail_before_native_discovery(self):
        urls = ("rerun+https://127.0.0.1:9876/proxy", "http://127.0.0.1:9876/proxy", "rerun+http://example.com:9876/proxy", "rerun+http://127.0.0.1/proxy", "rerun+http://127.0.0.1:0/proxy", "rerun+http://127.0.0.1:65536/proxy", "rerun+http://a@127.0.0.1:9876/proxy", "rerun+http://127.0.0.1:9876/proxy#", "rerun+http://127.0.0.1:9876/other", "rerun+http://127.0.0.1:9876/proxy\n")
        for url in urls:
            for target in (url, "example:" + url):
                with self.subTest(target=target), patch.object(cli, "native_binary") as native, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main([target]), 1)
                    native.assert_not_called()

    def test_removed_forms_and_invalid_numeric_options_are_rejected(self):
        for arguments in (["view", "example:/a.rrd"], ["--host", "example", "/a.rrd"], ["--config", "/config.toml", "example:/a.rrd"], ["--port", "0", "example:/a.rrd"], ["--port", "65536", "example:/a.rrd"], ["--port", "1.5", "example:/a.rrd"], ["--memory-bytes", "-1", "example:/a.rrd"], ["--cpu-count", "0", "example:/a.rrd"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.main(arguments)
            self.assertEqual(raised.exception.code, 2)

    def test_irrelevant_options_fail_before_native_discovery(self):
        with tempfile.TemporaryDirectory() as root:
            recording = Path(root) / "local.rrd"
            recording.touch()
            for target, option in ((str(recording), "--port"), (str(recording), "--cpu-count"), ("rerun+http://127.0.0.1:9876/proxy", "--port"), ("example:rerun+http://127.0.0.1:9876/proxy", "--memory-bytes")):
                with self.subTest(target=target, option=option), patch.object(cli, "native_binary") as native, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main([target, option, "1"]), 1)
                    native.assert_not_called()

    def test_remote_file_options_reach_owned_session(self):
        with patch.object(cli, "native_binary", return_value=Path("/native")), patch.object(cli, "remote_view", return_value=0) as view:
            self.assertEqual(cli.main(["example:/file.rrd", "--port", "43210", "--cpu-count", "1", "--memory-bytes", "1234", "--tasks-max", "45", "--runtime-seconds", "67"]), 0)
        self.assertEqual(view.call_args.args[3], {"cpu_count": 1, "memory_bytes": 1234, "tasks_max": 45, "runtime_seconds": 67})
        self.assertEqual(view.call_args.kwargs["port"], 43210)

    def test_remote_executable_falls_back_to_standard_uv_location(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home with 'quotes'"
            binary = home / ".local/bin/rerun-remote"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 42\n")
            binary.chmod(0o755)
            command = cli.remote_command("example", "/literal.rrd", cli.DEFAULT_RESOURCES)
            environment = dict(os.environ, HOME=str(home), PATH="/usr/bin:/bin")
            self.assertEqual(subprocess.run(["/bin/sh", "-c", command[-1]], env=environment, timeout=5).returncode, 42)
            binary.unlink()
            result = subprocess.run(["/bin/sh", "-c", command[-1]], env=environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 127)
            self.assertEqual(result.stdout, "")
            self.assertIn("rerun-remote[server] @ git+ssh://git@github.com/genzorr/rerun-remote.git", result.stderr)
            self.assertIn("uv tool install", result.stderr)


class TestVersion(unittest.TestCase):
    def test_platform_bundle_resolution_preserves_native_version_check(self) -> None:
        for platform, bundled, selected in (("darwin", True, "Rerun.app/Contents/MacOS/Rerun"), ("darwin", False, "rerun"), ("linux", True, "rerun")):
            with self.subTest(platform=platform, bundled=bundled), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                bare = root / "rerun"
                app = root / "Rerun.app/Contents/MacOS/Rerun"
                expected = root / selected
                bare.write_text("#!/bin/sh\necho 'rerun " + (cli.VERSION if expected == bare else "0.32.0") + "'\n")
                bare.chmod(0o755)
                if bundled:
                    app.parent.mkdir(parents=True)
                    app.write_text("#!/bin/sh\necho 'rerun " + (cli.VERSION if expected == app else "0.32.0") + "'\n")
                    app.chmod(0o755)
                with patch.object(cli.sys, "platform", platform), patch.object(cli.importlib.metadata, "version", return_value=cli.VERSION), patch.dict(sys.modules, {"rerun_cli": types.SimpleNamespace(__file__=str(root / "__init__.py"))}):
                    self.assertEqual(cli.native_binary(), expected)

    def test_macos_bundle_version_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            app = root / "Rerun.app/Contents/MacOS/Rerun"
            app.parent.mkdir(parents=True)
            app.write_text("#!/bin/sh\necho 'rerun 0.32.0'\n")
            app.chmod(0o755)
            with patch.object(cli.sys, "platform", "darwin"), patch.object(cli.importlib.metadata, "version", return_value=cli.VERSION), patch.dict(sys.modules, {"rerun_cli": types.SimpleNamespace(__file__=str(root / "__init__.py"))}):
                with self.assertRaisesRegex(cli.ViewError, "version differs"):
                    cli.native_binary()

    def test_missing_and_nonexecutable_macos_binary_report_selected_path(self) -> None:
        for bundled in (False, True):
            with self.subTest(bundled=bundled), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                expected = root / ("Rerun.app/Contents/MacOS/Rerun" if bundled else "rerun")
                if bundled:
                    expected.parent.mkdir(parents=True)
                    expected.write_text("not executable")
                with patch.object(cli.sys, "platform", "darwin"), patch.object(cli.importlib.metadata, "version", return_value=cli.VERSION), patch.dict(sys.modules, {"rerun_cli": types.SimpleNamespace(__file__=str(root / "__init__.py"))}):
                    with self.assertRaises(cli.ViewError) as raised:
                        cli.native_binary()
                self.assertIn(str(expected), str(raised.exception))

    def test_sdk_mismatch_requires_explicit_update(self):
        with patch.object(cli.importlib.metadata, "version", return_value="0.32.0"), patch.dict(sys.modules, {"rerun_cli": types.SimpleNamespace(__file__="/fake/__init__.py")}):
            with self.assertRaisesRegex(cli.ViewError, "expected 0.40.0"):
                cli.native_binary()

    def test_native_mismatch_requires_explicit_update(self):
        with tempfile.TemporaryDirectory() as root:
            native = Path(root) / "rerun"
            native.write_text("#!/bin/sh\necho 'rerun 0.32.0'\n")
            native.chmod(0o755)
            with patch.object(cli.importlib.metadata, "version", return_value=cli.VERSION), patch.dict(sys.modules, {"rerun_cli": types.SimpleNamespace(__file__=str(Path(root) / "__init__.py"))}):
                with self.assertRaisesRegex(cli.ViewError, "version differs"):
                    cli.native_binary()


class TestProcesses(unittest.TestCase):
    def spawn(self, source, **kwargs):
        return subprocess.Popen([sys.executable, "-c", source], start_new_session=True, **kwargs)

    def test_public_view_signals_retire_local_and_remote_children(self):
        for remote in (False, True):
            for signum in (signal.SIGTERM, signal.SIGHUP):
                with self.subTest(remote=remote, signum=signum), tempfile.TemporaryDirectory() as root:
                    root = Path(root)
                    recording = root / "recording.rrd"
                    recording.touch()
                    viewer_pid = root / "viewer-pid"
                    viewer_descendant = root / "viewer-descendant-retired"
                    ssh_pid = root / "ssh-pid"
                    ssh_descendant = root / "ssh-descendant-retired"
                    def child_source(marker):
                        return "import signal,time,pathlib; signal.signal(signal.SIGTERM,lambda *a:(pathlib.Path(" + repr(str(marker)) + ").touch(),exit(0))); print('child ready',flush=True); time.sleep(60)"
                    native = root / "native-viewer"
                    native.write_text('#!' + sys.executable + '\n' + "\n".join((
                        "import os,sys,subprocess,time",
                        "from pathlib import Path",
                        "child=subprocess.Popen([sys.executable,'-c'," + repr(child_source(viewer_descendant)) + "],stdout=subprocess.PIPE)",
                        "child.stdout.readline()",
                        "Path(" + repr(str(viewer_pid)) + ").write_text(str(os.getpid()))",
                        "time.sleep(60)",
                    )) + '\n')
                    native.chmod(0o755)
                    ready = json.dumps({"version": cli.VERSION, "segment_url": f"rerun+http://127.0.0.1:{cli.PORT}/catalog/selected/segment"})
                    ssh_source = "\n".join((
                        "import os,sys,subprocess,time",
                        "from pathlib import Path",
                        "child=subprocess.Popen([sys.executable,'-c'," + repr(child_source(ssh_descendant)) + "],stdout=subprocess.PIPE)",
                        "child.stdout.readline()",
                        "Path(" + repr(str(ssh_pid)) + ").write_text(str(os.getpid()))",
                        "print(" + repr(ready) + ",flush=True)",
                        "sys.stdin.read()",
                    ))
                    arguments = ["example:/recording.rrd"] if remote else [str(recording)]
                    source = "\n".join((
                        "from pathlib import Path",
                        "from unittest.mock import patch",
                        "from rerun_remote import cli",
                        "with patch.object(cli,'native_binary',return_value=Path(" + repr(str(native)) + ")), patch.object(cli,'port_available'), patch.object(cli,'remote_command',return_value=" + repr([sys.executable, "-c", ssh_source]) + "):",
                        "    raise SystemExit(cli.main(" + repr(arguments) + "))",
                    ))
                    process = self.spawn(source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        deadline = time.monotonic() + 5
                        while not viewer_pid.exists() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(viewer_pid.exists())
                        os.kill(process.pid, signum)
                        self.assertEqual(process.wait(timeout=20), 130)
                        self.assertTrue(viewer_descendant.exists())
                        if remote:
                            self.assertTrue(ssh_descendant.exists())
                        for file in (viewer_pid, ssh_pid) if remote else (viewer_pid,):
                            with self.assertRaises(ProcessLookupError):
                                os.kill(int(file.read_text()), 0)
                    finally:
                        cli.stop_process(process, grace=0.5)
                        for file in (viewer_pid, ssh_pid):
                            if file.exists():
                                try:
                                    os.killpg(int(file.read_text()), signal.SIGKILL)
                                except ProcessLookupError:
                                    pass
                        process.stdout.close()
                        process.stderr.close()

    def test_public_view_restores_prior_signal_handlers(self):
        before = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP)}
        with tempfile.TemporaryDirectory() as root:
            recording = Path(root) / "recording.rrd"
            recording.touch()
            with patch.object(cli, "native_binary", return_value=Path("/fake/native")), patch.object(cli, "local_view", return_value=0):
                self.assertEqual(cli.main([str(recording)]), 0)
        for signum, handler in before.items():
            self.assertIs(signal.getsignal(signum), handler)

    def test_owned_process_group_cleanup_retires_descendant(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "retired"
            source = "import signal,time,pathlib; signal.signal(signal.SIGTERM,lambda *a:(pathlib.Path(" + repr(str(marker)) + ").touch(),exit(0))); print('ready',flush=True); time.sleep(60)"
            parent = self.spawn("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(source) + "]); time.sleep(60)", stdout=subprocess.PIPE)
            try:
                self.assertTrue(select_ready(parent.stdout))
                parent.stdout.readline()
                cli.stop_process(parent)
                deadline = time.monotonic() + 2
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
            finally:
                cli.stop_process(parent)
                parent.stdout.close()

    def test_cleanup_retires_descendant_after_leader_exits(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "retired"
            source = "import signal,time,pathlib; signal.signal(signal.SIGTERM,lambda *a:(pathlib.Path(" + repr(str(marker)) + ").touch(),exit(0))); print('ready',flush=True); time.sleep(60)"
            parent = self.spawn("import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(source) + "])", stdout=subprocess.PIPE)
            try:
                self.assertTrue(select_ready(parent.stdout))
                parent.stdout.readline()
                parent.wait(timeout=5)
                cli.stop_process(parent, grace=0.5)
                self.assertTrue(marker.exists())
            finally:
                cli.stop_process(parent, grace=0.5)
                parent.stdout.close()

    def test_remote_startup_failure_cleans_ssh(self):
        ssh = self.spawn("import time; print('invalid readiness',flush=True); time.sleep(60)", stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        resources = cli.DEFAULT_RESOURCES
        with patch.object(cli, "port_available"), patch.object(cli.subprocess, "Popen", return_value=ssh), patch.object(cli, "viewer") as desktop:
            with self.assertRaisesRegex(cli.ViewError, "invalid readiness"):
                cli.remote_view(Path("/fake/native"), "example", "/recording.rrd", resources)
        self.assertIsNotNone(ssh.poll())
        desktop.assert_not_called()
        ssh.stdout.close()

    def test_worker_startup_failure_exits_cleanly_with_stdin_open(self):
        with tempfile.TemporaryDirectory() as root:
            recording = Path(root) / "invalid.rrd"
            recording.touch()
            native = Path(root) / "fake-native"
            native.write_text("#!/bin/sh\nprintf 'native actionable startup failure\\n' >&2\nexit 9\n")
            native.chmod(0o755)
            source = "\n".join((
                "import sys,types,resource",
                "from pathlib import Path",
                "from unittest.mock import patch",
                "from rerun_remote import cli",
                "resource.setrlimit(resource.RLIMIT_CORE,(0,0))",
                "sys.modules['rerun.catalog']=types.SimpleNamespace(CatalogClient=object)",
                "with patch.object(cli,'native_binary',return_value=Path(" + repr(str(native)) + ")), patch.object(cli.importlib.metadata,'version',return_value='54.0.0'), patch.object(cli,'verify_controls'), patch.object(cli,'port_available'):",
                "    raise SystemExit(cli.main(['_worker','--unit','owned.scope','--recording'," + repr(str(recording)) + "]))",
            ))
            process = self.spawn(source, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                status = process.wait(timeout=5)
                diagnostic = process.stderr.read().decode()
                self.assertEqual(status, 1)
                self.assertIn("Native remote server exited before readiness", diagnostic)
                self.assertIn("native actionable startup failure", diagnostic)
                self.assertLess(len(diagnostic), 5000)
                self.assertEqual(process.stdout.read(), b"")
                self.assertNotIn("Fatal Python error", diagnostic)
                self.assertFalse(process.stdin.closed)
            finally:
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()
                cli.stop_process(process)

    def test_connection_loss_retires_desktop(self):
        ready = json.dumps({"version": cli.VERSION, "segment_url": f"rerun+http://127.0.0.1:{cli.PORT}/catalog/selected/segment"})
        ssh = self.spawn("import time; print(" + repr(ready) + ",flush=True); time.sleep(0.4)", stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        desktop = self.spawn("import time; time.sleep(60)")
        resources = cli.DEFAULT_RESOURCES
        try:
            with patch.object(cli, "port_available"), patch.object(cli.subprocess, "Popen", return_value=ssh), patch.object(cli, "viewer", return_value=desktop):
                with self.assertRaisesRegex(cli.ViewError, "SSH connection ended"):
                    cli.remote_view(Path("/fake/native"), "example", "/recording.rrd", resources)
            self.assertIsNotNone(desktop.poll())
            self.assertIsNotNone(ssh.poll())
        finally:
            cli.stop_process(desktop)
            cli.stop_process(ssh)
            ssh.stdout.close()

    def test_viewer_closure_sends_eof_to_remote_session(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "eof"
            ready = json.dumps({"version": cli.VERSION, "segment_url": f"rerun+http://127.0.0.1:{cli.PORT}/catalog/selected/segment"})
            ssh = self.spawn("import sys,pathlib; print(" + repr(ready) + ",flush=True); sys.stdin.read(); pathlib.Path(" + repr(str(marker)) + ").touch()", stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            desktop = self.spawn("pass")
            resources = cli.DEFAULT_RESOURCES
            with patch.object(cli, "port_available"), patch.object(cli.subprocess, "Popen", return_value=ssh), patch.object(cli, "viewer", return_value=desktop):
                self.assertEqual(cli.remote_view(Path("/fake/native"), "example", "/recording.rrd", resources), 0)
            self.assertTrue(marker.exists())
            self.assertIsNotNone(ssh.poll())
            ssh.stdout.close()

    def test_readiness_mismatch_and_nonloopback_are_rejected(self):
        for response, expected in (({"version": "0.32.0", "segment_url": "irrelevant"}, "Remote Rerun version"), ({"version": cli.VERSION, "segment_url": "rerun+http://other-host:38886/segment"}, "invalid readiness")):
            process = self.spawn("import time; print(" + repr(json.dumps(response)) + ",flush=True); time.sleep(60)", stdout=subprocess.PIPE)
            try:
                with self.subTest(response=response), self.assertRaisesRegex(cli.ViewError, expected):
                    cli.readiness(process, timeout=2)
            finally:
                cli.stop_process(process)
                process.stdout.close()

    def test_local_interrupt_retires_desktop(self):
        process = self.spawn("import time; time.sleep(60)")
        original_wait = process.wait
        interrupted = False
        def wait(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_wait(*args, **kwargs)
        with patch.object(cli, "viewer", return_value=process), patch.object(process, "wait", side_effect=wait):
            with self.assertRaises(KeyboardInterrupt):
                cli.local_view(Path("/fake/native"), "/recording.rrd")
        self.assertIsNotNone(process.poll())

    def test_live_failure_retires_only_owned_viewer_and_tunnel(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                producer = self.spawn("import time; time.sleep(60)")
                desktop = self.spawn("import time; time.sleep(60)")
                ssh = self.spawn("import time; time.sleep(60)") if remote else None
                try:
                    with patch.object(cli, "port_available"), patch.object(cli, "viewer_listener_owned", return_value=True), patch.object(cli.subprocess, "Popen", return_value=ssh) as launch, patch.object(cli, "viewer", return_value=desktop), patch.object(cli, "initialize_time", side_effect=cli.ViewError("upstream unavailable")):
                        with self.assertRaisesRegex(cli.ViewError, "upstream unavailable"):
                            cli.live_view(Path("/native"), "rerun+http://127.0.0.1:9876/proxy?name=a%20b", host="example" if remote else None, port=43210)
                        if remote:
                            command = launch.call_args.args[0]
                            self.assertIn("-N", command)
                            self.assertEqual(command[-1], "example")
                            self.assertIn("127.0.0.1:43210:127.0.0.1:9876", command)
                        else:
                            launch.assert_not_called()
                    self.assertIsNotNone(desktop.poll())
                    if ssh is not None:
                        self.assertIsNotNone(ssh.poll())
                    self.assertIsNone(producer.poll())
                finally:
                    cli.stop_process(desktop)
                    if ssh is not None:
                        cli.stop_process(ssh)
                    cli.stop_process(producer)

    def test_remote_live_forwards_url_path_and_query_and_retires_on_ssh_loss(self):
        ssh = self.spawn("import time; time.sleep(.4)")
        desktop = self.spawn("import time; time.sleep(60)")
        try:
            with patch.object(cli, "port_available"), patch.object(cli, "viewer_listener_owned", return_value=True), patch.object(cli.subprocess, "Popen", return_value=ssh), patch.object(cli, "viewer", return_value=desktop) as view, patch.object(cli, "initialize_time"):
                with self.assertRaisesRegex(cli.ViewError, "SSH connection ended"):
                    cli.live_view(Path("/native"), "rerun+http://127.0.0.1:9876/proxy?name=a%20b&x=1", host="example", port=43210)
                self.assertEqual(view.call_args.args[1], "rerun+http://127.0.0.1:43210/proxy?name=a%20b&x=1")
            self.assertIsNotNone(desktop.poll())
        finally:
            cli.stop_process(desktop)
            cli.stop_process(ssh)

    def test_custom_readiness_port_accepts_only_matching_owned_server_address(self):
        for port, expected in ((43210, True), (38886, False)):
            response = json.dumps({"version": cli.VERSION, "segment_url": f"rerun+http://127.0.0.1:{port}/catalog/selected/segment", "segment_id": "selected"})
            process = self.spawn("import time; print(" + repr(response) + ",flush=True); time.sleep(60)", stdout=subprocess.PIPE)
            try:
                if expected:
                    self.assertEqual(cli.readiness(process, timeout=2, port=43210), (f"rerun+http://127.0.0.1:{port}/catalog/selected/segment", "selected"))
                else:
                    with self.assertRaisesRegex(cli.ViewError, "invalid readiness"):
                        cli.readiness(process, timeout=2, port=43210)
            finally:
                cli.stop_process(process)
                process.stdout.close()


def select_ready(stream):
    import select
    return select.select([stream], [], [], 5)[0]


class TestCatalog(unittest.TestCase):
    def catalog(self, error, ids):
        from unittest.mock import Mock
        blueprint = Mock()
        blueprint.register.return_value.wait.side_effect = error
        blueprint.register.return_value.wait.return_value.segment_ids = ids
        blueprint.segment_ids.return_value = ids
        dataset = Mock()
        dataset.register.return_value.wait.return_value.segment_ids = ["recording"]
        dataset.segment_ids.return_value = ["recording"]
        dataset.blueprint_dataset.return_value = blueprint
        fresh = Mock()
        fresh.default_blueprint.return_value = None
        fresh.blueprints.return_value = []
        fresh.segment_url.return_value = "private-result"
        client = Mock()
        client.create_dataset.return_value = dataset
        client.get_dataset.return_value = fresh
        return client, dataset

    def test_missing_saved_blueprint_still_selects_recording(self):
        import threading
        client, dataset = self.catalog(None, [])
        self.assertEqual(cli.catalog_recording(client, Path("/recording.rrd"), threading.Event()), ("private-result", "recording"))
        dataset.set_default_blueprint.assert_not_called()

    def test_registration_errors_are_not_masked(self):
        import threading
        for error, ids in ((ValueError("invalid recording"), []), (ValueError("registration failed"), ["unexpected"])):
            client, dataset = self.catalog(error, ids)
            with self.subTest(error=error, ids=ids), self.assertRaises(ValueError):
                cli.catalog_recording(client, Path("/recording.rrd"), threading.Event())
            dataset.set_default_blueprint.assert_not_called()

    def test_disconnected_startup_does_not_mutate_catalog(self):
        import threading
        disconnected = threading.Event()
        disconnected.set()
        client, _ = self.catalog(None, [])
        with self.assertRaisesRegex(cli.ViewError, "connection closed"):
            cli.catalog_recording(client, Path("/recording.rrd"), disconnected)
        client.create_dataset.assert_not_called()

    def test_saved_blueprint_is_default_with_fresh_readback(self):
        import threading
        client, dataset = self.catalog(None, ["layout"])
        dataset.blueprint_dataset.return_value.register.return_value.wait.return_value.segment_ids = ["layout"]
        client.get_dataset.return_value.default_blueprint.return_value = "layout"
        client.get_dataset.return_value.blueprints.return_value = ["layout"]
        self.assertEqual(cli.catalog_recording(client, Path("/recording.rrd"), threading.Event()), ("private-result", "recording"))
        dataset.set_default_blueprint.assert_called_once_with("layout")
        client.get_dataset.return_value.default_blueprint.return_value = "wrong-layout"
        with self.assertRaisesRegex(cli.ViewError, "fresh catalog readback"):
            cli.catalog_recording(client, Path("/recording.rrd"), threading.Event())


class TestStartupTime(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        import numpy as np
        self.numpy = np
        self.desktop = Mock(pid=os.getpid(), returncode=None)
        self.desktop.poll.return_value = None
        self.client = Mock()
        self.client_type = Mock()
        self.client_type.connect.return_value = self.client
        self.module = types.SimpleNamespace(ViewerClient=self.client_type)

    def state(self, *, loading=False, store="Recording:application:selected", time_type="duration", cursor=0, version=cli.VERSION, timeline="arrival", start=-(2**63) + 1, end=2**63 - 1):
        recording = types.SimpleNamespace(store_id=store, timelines=[types.SimpleNamespace(name=timeline, time_type=time_type, start=start, end=end)], current_timeline=timeline, current_time=cursor)
        return types.SimpleNamespace(viewer_version=version, loading=["source"] if loading else [], recordings=[recording])

    def initialize(self, requested, **kwargs):
        with patch.dict(sys.modules, {"rerun.experimental": self.module}), patch.object(cli, "viewer_listener_owned", return_value=True):
            return cli.apply_startup_time(self.desktop.pid, 32123, requested, timeout=kwargs.pop("timeout", 1), **kwargs)

    def test_exact_duration_and_timestamp_pause_selected_store_once(self):
        np = self.numpy
        requested = 1234567890123456789
        for kind, dtype in (("duration", np.dtype("timedelta64[ns]")), ("timestamp", np.dtype("datetime64[ns]"))):
            with self.subTest(kind=kind):
                state = self.state(time_type=kind, store=r"Recording:catalog\:name\\part:selected:opaque")
                self.client.reset_mock()
                self.client.viewer_state.side_effect = [self.state(loading=True), state, state]
                def set_time(timeline, *, play, recording, **time):
                    self.assertFalse(play)
                    self.assertEqual(recording, state.recordings[0].store_id)
                    self.assertEqual(timeline, "arrival")
                    value = time[kind]
                    self.assertEqual(value.dtype, dtype)
                    self.assertEqual(int(value.astype("int64")), requested)
                    state.recordings[0].current_time = int(value.astype("int64"))
                self.client.set_time.side_effect = set_time
                self.initialize(("arrival", requested), selected_segment="selected:opaque")
                self.client.set_time.assert_called_once()
                state.recordings[0].current_time = requested + 1
                self.client.set_time.assert_called_once()

    def test_wrong_recording_and_invalid_timeline_never_seek(self):
        cases = ((self.state(store="Recording:application:foreign"), "different recording", {"selected_segment": "selected"}), (self.state(timeline="other"), "unavailable", {}), (self.state(time_type="sequence"), "duration or timestamp", {}), (self.state(start=0, end=10), "outside", {}), (self.state(start=None), "outside", {}), (self.state(version="0.38.0"), "expected 0.40.0", {}))
        for state, diagnostic, kwargs in cases:
            with self.subTest(diagnostic=diagnostic):
                self.client.reset_mock()
                self.client.viewer_state.side_effect = None
                self.client.viewer_state.return_value = state
                with self.assertRaisesRegex(cli.ViewError, diagnostic):
                    self.initialize(("arrival", 11), **kwargs)
                self.client.set_time.assert_not_called()

    def test_ambiguous_local_recordings_never_seek(self):
        state = self.state()
        state.recordings.append(self.state(store="Recording:application:second").recordings[0])
        self.client.viewer_state.return_value = state
        with self.assertRaisesRegex(cli.ViewError, "exactly the selected recording"):
            self.initialize(("arrival", 10))
        self.client.set_time.assert_not_called()

    def test_exact_cursor_readback_is_required_without_repeating_seek(self):
        self.client.viewer_state.return_value = self.state(cursor=9)
        with self.assertRaisesRegex(cli.ViewError, "exact cursor readback"):
            self.initialize(("arrival", 10), timeout=0.25)
        self.client.set_time.assert_called_once()

    def test_live_readiness_waits_for_recording_without_seeking(self):
        state = self.state(loading=True)
        empty = self.state(loading=True)
        empty.recordings = []
        self.client.viewer_state.side_effect = [empty, state]
        self.initialize(None, live=True)
        self.client.set_time.assert_not_called()

    def test_live_readiness_accepts_multiple_recordings_without_time(self):
        state = self.state(loading=True)
        state.recordings.append(self.state(store="Recording:application:second").recordings[0])
        self.client.viewer_state.return_value = state
        self.initialize(None, live=True)
        self.client.set_time.assert_not_called()

    def test_live_time_waits_for_future_data_then_seeks_once(self):
        waiting = self.state(loading=True, end=9)
        ready = self.state(loading=True, end=12, cursor=10)
        self.client.viewer_state.side_effect = [waiting, ready, ready]
        self.initialize(("arrival", 10), live=True)
        self.client.set_time.assert_called_once()

    def test_unavailable_live_endpoint_is_bounded_without_seeking(self):
        state = self.state(loading=True)
        state.recordings = []
        self.client.viewer_state.return_value = state
        with self.assertRaisesRegex(cli.ViewError, "live endpoint readiness timed out"):
            self.initialize(None, live=True, timeout=.15)
        self.client.set_time.assert_not_called()

    def test_loading_timeout_never_seeks(self):
        self.client.viewer_state.return_value = self.state(loading=True)
        with self.assertRaisesRegex(cli.ViewError, "timed out"):
            self.initialize(("arrival", 10), timeout=0.15)
        self.client.set_time.assert_not_called()

    def test_blocked_control_helper_is_bounded_and_retired(self):
        helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.PIPE, start_new_session=True)
        try:
            with patch.object(cli.subprocess, "Popen", return_value=helper):
                with self.assertRaisesRegex(cli.ViewError, "timed out"):
                    cli.initialize_time(self.desktop, 32123, ("arrival", 10), timeout=0.15)
            self.assertIsNotNone(helper.poll())
        finally:
            cli.stop_process(helper)

    def test_interruption_retires_control_helper(self):
        helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.PIPE, start_new_session=True)
        try:
            original_sleep = time.sleep
            interrupted = False
            def sleep(seconds):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt
                original_sleep(seconds)
            with patch.object(cli.subprocess, "Popen", return_value=helper), patch.object(cli.time, "sleep", side_effect=sleep):
                with self.assertRaises(KeyboardInterrupt):
                    cli.initialize_time(self.desktop, 32123, ("arrival", 10), timeout=1)
            self.assertIsNotNone(helper.poll())
        finally:
            cli.stop_process(helper)

    def test_process_exit_or_ssh_loss_retires_blocked_control_helper(self):
        from unittest.mock import Mock
        for failure in ("desktop", "ssh"):
            with self.subTest(failure=failure):
                helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.PIPE, start_new_session=True)
                ssh = Mock(returncode=255)
                ssh.poll.return_value = None
                self.desktop.poll.side_effect = [None, 9] if failure == "desktop" else None
                ssh.poll.side_effect = [None, 255] if failure == "ssh" else None
                try:
                    with patch.object(cli.subprocess, "Popen", return_value=helper):
                        with self.assertRaisesRegex(cli.ViewError, "viewer exited" if failure == "desktop" else "SSH connection ended"):
                            cli.initialize_time(self.desktop, 32123, ("arrival", 10), ssh=ssh, timeout=1)
                    self.assertIsNotNone(helper.poll())
                finally:
                    cli.stop_process(helper)

    def test_foreign_listener_cannot_receive_control_calls(self):
        with socket.socket() as foreign:
            foreign.bind(("127.0.0.1", 0))
            foreign.listen()
            desktop = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            try:
                with patch.dict(sys.modules, {"rerun.experimental": self.module}):
                    with self.assertRaisesRegex(cli.ViewError, "timed out"):
                        cli.apply_startup_time(desktop.pid, foreign.getsockname()[1], ("arrival", 10), timeout=0.15)
                self.client_type.connect.assert_not_called()
                self.client.set_time.assert_not_called()
            finally:
                cli.stop_process(desktop)

    def test_owned_loopback_listener_allows_exact_cursor_initialization(self):
        port = cli.control_port()
        source = "import socket,time; s=socket.socket(); s.bind(('127.0.0.1'," + str(port) + ")); s.listen(); print('ready',flush=True); time.sleep(60)"
        desktop = subprocess.Popen([sys.executable, "-c", source], stdout=subprocess.PIPE, start_new_session=True)
        try:
            self.assertTrue(select_ready(desktop.stdout))
            desktop.stdout.readline()
            state = self.state(cursor=10)
            self.client.viewer_state.return_value = state
            with patch.dict(sys.modules, {"rerun.experimental": self.module}):
                cli.apply_startup_time(desktop.pid, port, ("arrival", 10), timeout=1)
            self.client.set_time.assert_called_once()
        finally:
            cli.stop_process(desktop)
            desktop.stdout.close()

    def test_startup_failure_cleans_local_viewer(self):
        desktop = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        with patch.object(cli, "viewer", return_value=desktop), patch.object(cli, "initialize_time", side_effect=cli.ViewError("invalid timeline")):
            with self.assertRaisesRegex(cli.ViewError, "invalid timeline"):
                cli.local_view(Path("/fake/native"), "/recording.rrd", ("arrival", 10))
        self.assertIsNotNone(desktop.poll())

    def test_startup_failure_cleans_remote_viewer_and_sends_ssh_eof(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "ssh-eof"
            ready = json.dumps({"version": cli.VERSION, "segment_url": f"rerun+http://127.0.0.1:{cli.PORT}/catalog/selected/segment", "segment_id": "selected"})
            source = "import sys,pathlib; print(" + repr(ready) + ",flush=True); sys.stdin.read(); pathlib.Path(" + repr(str(marker)) + ").touch()"
            ssh = subprocess.Popen([sys.executable, "-c", source], stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
            desktop = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            try:
                resources = cli.DEFAULT_RESOURCES
                with patch.object(cli, "port_available"), patch.object(cli.subprocess, "Popen", return_value=ssh), patch.object(cli, "viewer", return_value=desktop), patch.object(cli, "initialize_time", side_effect=cli.ViewError("invalid timeline")):
                    with self.assertRaisesRegex(cli.ViewError, "invalid timeline"):
                        cli.remote_view(Path("/fake/native"), "example", "/recording.rrd", resources, ("arrival", 10))
                self.assertIsNotNone(desktop.poll())
                self.assertIsNotNone(ssh.poll())
                self.assertTrue(marker.exists())
            finally:
                cli.stop_process(desktop)
                cli.retire_ssh(ssh)
                ssh.stdout.close()

    def test_real_initializer_subprocess_preserves_ns_and_saved_state(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            saved = root / "viewer-state.json"
            saved.write_text(json.dumps({"cursor": 0, "seeks": 0, "panels": {"Time": False, "Blueprint": False, "Selection": False}}))
            package = root / "rerun"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "experimental.py").write_text("\n".join((
                "import json,time",
                "from pathlib import Path",
                "from types import SimpleNamespace as S",
                "FILE=Path(" + repr(str(saved)) + ")",
                "class ViewerClient:",
                "    @classmethod",
                "    def connect(cls,url): return cls()",
                "    def viewer_state(self):",
                "        state=json.loads(FILE.read_text())",
                "        recording=S(store_id='Recording:application:selected',timelines=[S(name='-arrival',time_type='timestamp',start=0,end=2**63-1)],current_timeline='-arrival',current_time=state['cursor'])",
                "        return S(viewer_version='0.40.0',loading=[],recordings=[recording])",
                "    def set_time(self,timeline,*,timestamp,play,recording):",
                "        assert timeline=='-arrival' and recording=='Recording:application:selected' and play is False",
                "        assert str(timestamp.dtype)=='datetime64[ns]'",
                "        state=json.loads(FILE.read_text()); state['cursor']=int(timestamp.astype('int64')); state['seeks']+=1; FILE.write_text(json.dumps(state))",
            )) + "\n")
            port = cli.control_port()
            desktop = subprocess.Popen([sys.executable, "-c", "import socket,time; s=socket.socket(); s.bind(('127.0.0.1'," + str(port) + ")); s.listen(); print('ready',flush=True); time.sleep(60)"], stdout=subprocess.PIPE, start_new_session=True)
            try:
                self.assertTrue(select_ready(desktop.stdout))
                desktop.stdout.readline()
                pythonpath = str(root) + os.pathsep + str(Path(cli.__file__).resolve().parents[1])
                with patch.dict(os.environ, {"PYTHONPATH": pythonpath}):
                    cli.initialize_time(desktop, port, ("-arrival", 1234567890123456789), selected_segment="selected", timeout=5)
                state = json.loads(saved.read_text())
                self.assertEqual(state["cursor"], 1234567890123456789)
                self.assertEqual(state["seeks"], 1)
                self.assertEqual(state["panels"], {"Time": False, "Blueprint": False, "Selection": False})
                (package / "experimental.py").write_text((package / "experimental.py").read_text().replace("state=json.loads(FILE.read_text())\n        recording", "FILE.with_suffix('.rpc-started').touch(); time.sleep(60)\n        state=json.loads(FILE.read_text())\n        recording"))
                with patch.dict(os.environ, {"PYTHONPATH": pythonpath}):
                    with self.assertRaisesRegex(cli.ViewError, "timed out"):
                        cli.initialize_time(desktop, port, ("-arrival", 1234567890123456789), selected_segment="selected", timeout=1)
                self.assertTrue(saved.with_suffix(".rpc-started").exists())
                self.assertIsNone(desktop.poll())
                self.assertEqual(json.loads(saved.read_text())["seeks"], 1)
            finally:
                cli.stop_process(desktop)
                desktop.stdout.close()


if __name__ == "__main__":
    unittest.main()
