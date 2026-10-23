import contextlib
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from rerun_remote import _bootstrap as setup, cli


def source_zip(payload=None):
    payload = setup.source_payload() if payload is None else payload
    length = struct.unpack("!I", payload[:4])[0]
    return payload[4 + length:]


class TestPayload(unittest.TestCase):
    def read(self, data):
        with tempfile.TemporaryFile() as stream:
            stream.write(data)
            stream.seek(0)
            return setup.read_payload(stream.fileno(), timeout=0.2)

    def test_framing_preserves_open_stdin_for_lifecycle(self):
        payload = setup.source_payload()
        with tempfile.TemporaryFile() as stream:
            stream.write(payload + b"lifecycle")
            stream.seek(0)
            manifest, source = setup.read_payload(stream.fileno())
            self.assertEqual(manifest, setup.profile_manifest(source))
            self.assertEqual(stream.read(), b"lifecycle")

    def test_identity_uses_actual_source_and_exact_profile(self):
        original = source_zip()
        manifest = setup.profile_manifest(original)
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as changed:
                for entry in archive.infolist():
                    content = archive.read(entry)
                    changed.writestr(entry, content + (b"\n# changed source\n" if entry.filename.endswith("cli.py") else b""))
        modified = output.getvalue()
        setup.validate_source(modified)
        self.assertNotEqual(setup.profile_identity(manifest), setup.profile_identity(setup.profile_manifest(modified)))
        for key, value in (("schema", 2), ("python", "3.13"), ("requirements", ["rerun-sdk==0.39.0"])):
            self.assertNotEqual(setup.profile_identity(manifest), setup.profile_identity(dict(manifest, **{key: value})))

    def test_rejects_size_hash_requirements_and_truncation(self):
        payload = setup.source_payload()
        length = struct.unpack("!I", payload[:4])[0]
        header = json.loads(payload[4:4 + length])
        for field, value in (("size", setup.MAX_PAYLOAD + 1), ("size", True), ("source_sha256", "0" * 64), ("requirements", ["bad @ https://example.invalid/source.tar.gz"])):
            invalid = json.dumps(dict(header, **{field: value})).encode()
            with self.subTest(field=field), self.assertRaises(setup.SetupError):
                self.read(struct.pack("!I", len(invalid)) + invalid + payload[4 + length:])
        for invalid in (struct.pack("!I", setup.MAX_HEADER + 1), payload[:-1], b""):
            with self.assertRaises(setup.SetupError):
                self.read(invalid)

    def test_rejects_unsafe_duplicate_and_compressed_entries(self):
        original = source_zip()
        for name, compression in (("../escape.py", zipfile.ZIP_STORED), ("rerun_remote/../escape.py", zipfile.ZIP_STORED), ("/escape.py", zipfile.ZIP_STORED), ("rerun_remote/cli.py", zipfile.ZIP_STORED), ("rerun_remote/extra.py", zipfile.ZIP_DEFLATED)):
            output = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(original)) as archive, zipfile.ZipFile(output, "w") as invalid:
                for entry in archive.infolist():
                    invalid.writestr(entry, archive.read(entry))
                with contextlib.redirect_stderr(io.StringIO()):
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", UserWarning)
                        invalid.writestr(name, b"pass", compress_type=compression)
            with self.subTest(name=name, compression=compression), self.assertRaises(setup.SetupError):
                setup.validate_source(output.getvalue())

    def test_validation_and_module_execution_ignore_conflicting_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "profile"
            profile.mkdir()
            (profile / "source.zip").write_bytes(source_zip())
            conflicting = Path(temporary) / "cwd/rerun_remote"
            conflicting.mkdir(parents=True)
            (conflicting / "__init__.py").write_text("raise RuntimeError('wrong package from current directory')")
            environment = setup.runtime_environment(profile)
            for arguments in (["-c", "import rerun_remote.cli as c; print(c.__file__)"], ["-m", "rerun_remote", "--help"]):
                result = subprocess.run([sys.executable, *arguments], env=environment, cwd=conflicting.parent, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("wrong package", result.stderr)
                self.assertIn("rerun_remote/cli.py" if arguments[0] == "-c" else "SSH_ALIAS", result.stdout)

    def test_remote_stdlib_bootstrap_ignores_shadowing_cwd_and_pythonpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "json.py").write_text("raise RuntimeError('wrong json from current directory')")
            command = cli.remote_command("example", "/missing.rrd", cli.DEFAULT_RESOURCES)[-1]
            result = subprocess.run(["/bin/sh", "-c", command], cwd=directory, env=dict(os.environ, PYTHONPATH=str(directory)), input=b"", capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"closed during source transfer", result.stderr)
            self.assertNotIn(b"wrong json", result.stderr)

    def test_blocked_payload_writes_are_bounded_and_keep_descriptor_usable(self):
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(10)"], stdin=subprocess.PIPE, start_new_session=True)
        try:
            with self.assertRaisesRegex(setup.SetupError, "transfer timed out"):
                setup.transfer(child, b"x" * setup.MAX_PAYLOAD, timeout=0.1)
            self.assertTrue(os.get_blocking(child.stdin.fileno()))
        finally:
            child.stdin.close()
            cli.stop_process(child, grace=0.1)


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "cache"
        self.root.mkdir()
        self.calls = self.directory / "calls.jsonl"
        self.failure = self.directory / "fail-install"
        self.bad_version = self.directory / "bad-version"
        native = self.directory / "rerun"
        native.write_text("#!/bin/sh\nprintf 'rerun 0.40.0\\n'\n")
        native.chmod(0o700)
        wrapper = "\n".join((
            "#!" + sys.executable,
            "import importlib.metadata,sys,types",
            "from pathlib import Path",
            "sys.version_info=(3,12,0)",
            "importlib.metadata.version=lambda name: ('0.1' if Path(" + repr(str(self.bad_version)) + ").exists() else {'rerun-sdk':'0.40.0','datafusion':'54.0.0'}[name])",
            "sys.modules['rerun_cli']=types.SimpleNamespace(__file__=" + repr(str(self.directory / "__init__.py")) + ")",
            "exec(sys.argv[2])",
        )) + "\n"
        self.uv = self.directory / "uv"
        self.uv.write_text("\n".join((
            "#!" + sys.executable,
            "import json,sys",
            "from pathlib import Path",
            "with Path(" + repr(str(self.calls)) + ").open('a') as output: output.write(json.dumps(sys.argv[1:])+'\\n')",
            "if sys.argv[1]=='venv':",
            " directory=Path(sys.argv[-1])/'bin'; directory.mkdir(parents=True)",
            " python=directory/'python'; python.write_text(" + repr(wrapper) + "); python.chmod(0o700)",
            "elif Path(" + repr(str(self.failure)) + ").exists():",
            " print('injected dependency install failure',file=sys.stderr);sys.exit(9)",
        )) + "\n")
        self.uv.chmod(0o700)
        self.source = source_zip()
        self.manifest = setup.profile_manifest(self.source)
        discover = patch.object(setup, "find_uv", return_value=str(self.uv))
        discover.start()
        self.addCleanup(discover.stop)

    def prepare(self, source=None, lifecycle=None):
        source = self.source if source is None else source
        lifecycle = setup.Lifecycle() if lifecycle is None else lifecycle
        return setup.prepare_runtime(self.root, setup.profile_manifest(source), source, lifecycle)


class TestRuntime(RuntimeFixture):
    def test_cold_validates_and_publishes_then_warm_avoids_uv_and_network(self):
        target = self.prepare()
        self.assertEqual(target.name, setup.profile_identity(self.manifest))
        self.assertEqual(json.loads((target / "complete.json").read_text()), self.manifest)
        self.assertEqual((target / "source.zip").read_bytes(), self.source)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        with patch.object(setup, "uv_executable", side_effect=AssertionError("warm must avoid uv")), patch.object(setup, "download_uv", side_effect=AssertionError("warm must avoid network")):
            self.assertEqual(self.prepare(), target)
        self.assertEqual(list((self.root / "staging").iterdir()), [])
        self.assertEqual((target.stat().st_mode & 0o777), 0o700)
        self.assertTrue((self.root / "locks" / (target.name + ".lock")).exists())

    def test_shared_cache_keeps_architecture_profiles_separate_and_reusable(self):
        targets = []
        for architecture in ("x86_64", "aarch64"):
            with patch.object(setup.os, "uname", return_value=type("Uname", (), {"machine": architecture})()), patch.object(setup.sys, "platform", "linux"):
                targets.append(self.prepare())
        self.assertNotEqual(targets[0], targets[1])
        self.assertEqual(len(self.calls.read_text().splitlines()), 4)
        for target in targets:
            self.assertEqual(json.loads((target / "complete.json").read_text()), self.manifest)
            self.assertEqual((target / "source.zip").read_bytes(), self.source)
            self.assertTrue((self.root / "locks" / (target.name + ".lock")).is_file())
        with patch.object(setup.os, "uname", return_value=type("Uname", (), {"machine": "x86_64"})()), patch.object(setup.sys, "platform", "linux"), patch.object(setup, "uv_executable", side_effect=AssertionError("existing architecture must reuse its runtime")):
            self.assertEqual(self.prepare(), targets[0])
        self.assertTrue((targets[1] / "complete.json").is_file())

    def test_failed_install_retry_has_fresh_staging_and_preserves_other_profiles(self):
        unrelated = self.root / "unrelated-installation"
        unrelated.write_text("preserve")
        self.failure.touch()
        with self.assertRaisesRegex(setup.SetupError, "injected dependency install failure"):
            self.prepare()
        self.assertEqual(list((self.root / "profiles").iterdir()), [])
        self.assertEqual(list((self.root / "staging").iterdir()), [])
        self.failure.unlink()
        first = self.prepare()
        second = self.prepare(self.source + b"changed source bytes")
        self.assertNotEqual(first, second)
        self.assertTrue((first / "complete.json").is_file())
        self.assertEqual(unrelated.read_text(), "preserve")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        venv_paths = [call[-1] for call in calls if call[0] == "venv"]
        self.assertEqual(len(set(venv_paths)), 3)

    def test_wrong_runtime_version_is_never_published(self):
        self.bad_version.touch()
        with self.assertRaisesRegex(setup.SetupError, "SDK version mismatch"):
            self.prepare()
        self.assertEqual(list((self.root / "profiles").iterdir()), [])
        self.assertEqual(list((self.root / "staging").iterdir()), [])

    def test_concurrent_first_use_produces_one_complete_install(self):
        results, errors = [], []
        def prepare():
            try:
                results.append(self.prepare())
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=prepare) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_disconnect_cancels_lock_wait_without_unlinking_shared_lock(self):
        read, write = os.pipe()
        waiter = setup.Lifecycle(read)
        self.addCleanup(os.close, read)
        self.addCleanup(waiter.close)
        identity = setup.profile_identity(self.manifest)
        result = []
        def wait():
            try:
                with setup.profile_lock(self.root, identity, waiter):
                    result.append("unexpected acquisition")
            except setup.SetupError as error:
                result.append(str(error))
        with setup.profile_lock(self.root, identity, setup.Lifecycle()):
            lock = self.root / "locks" / (identity + ".lock")
            inode = lock.stat().st_ino
            thread = threading.Thread(target=wait)
            thread.start()
            os.close(write)
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(lock.stat().st_ino, inode)
        self.assertIn("SSH connection closed", result[0])
        with setup.profile_lock(self.root, identity, setup.Lifecycle()):
            self.assertEqual(lock.stat().st_ino, inode)

    def test_disconnect_retires_installer_and_descendant(self):
        read, write = os.pipe()
        lifecycle = setup.Lifecycle(read)
        self.addCleanup(os.close, read)
        self.addCleanup(lifecycle.close)
        pidfile = self.directory / "child.pid"
        source = "import subprocess,sys,time;from pathlib import Path;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);Path(" + repr(str(pidfile)) + ").write_text(str(child.pid));time.sleep(30)"
        result = []
        def run():
            try:
                lifecycle.run([sys.executable, "-c", source])
            except setup.SetupError as error:
                result.append(str(error))
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(pidfile.exists())
        os.close(write)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIn("SSH connection closed", result[0])
        pid = int(pidfile.read_text())
        if Path(f"/proc/{pid}/stat").exists():
            self.assertEqual(Path(f"/proc/{pid}/stat").read_text().split()[2], "Z")

    def test_missing_or_mismatched_resource_controls_prevent_setup(self):
        argv = ["--controlled", "owned.scope", str(self.directory / "source.zip"), json.dumps(self.manifest), "--recording", "/missing.rrd"]
        with patch.object(cli, "verify_controls", side_effect=cli.ViewError("effective controls missing")), patch.object(setup, "prepare_runtime") as prepare, patch.object(sys.stdin, "fileno", return_value=None), contextlib.redirect_stderr(io.StringIO()) as diagnostic:
            self.assertEqual(setup.main(argv), 1)
        self.assertIn("effective controls missing", diagnostic.getvalue())
        prepare.assert_not_called()


class TestUv(unittest.TestCase):
    def archive(self, root, *, unsafe=False):
        path = root / "fixture.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            entry = tarfile.TarInfo("uv-x86_64-unknown-linux-gnu/uv")
            if unsafe:
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc/passwd"
                archive.addfile(entry)
            else:
                content = b"#!/bin/sh\nprintf 'fixture uv\\n'\n"
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        return path

    @unittest.skipUnless(shutil.which("uv"), "Requires uv")
    def test_real_uv_accepts_bootstrap_managed_python_options(self):
        class CheckedOptions(Exception):
            pass
        with tempfile.TemporaryDirectory() as temporary:
            source = source_zip()
            def run(command, *, env):
                # Disable downloads while asking real uv to parse the production command/environment.
                result = subprocess.run(command, env=dict(env, UV_PYTHON_DOWNLOADS="never"), capture_output=True, text=True, timeout=20)
                self.assertNotIn("cannot be used with", result.stderr)
                if result.returncode:
                    self.assertIn("managed Python", result.stderr)
                raise CheckedOptions
            with patch.object(setup, "find_uv", return_value=shutil.which("uv")), patch.object(setup.Lifecycle, "run", side_effect=run), patch.dict(os.environ, {"UV_PYTHON_PREFERENCE": "only-managed"}):
                with self.assertRaises(CheckedOptions):
                    setup.prepare_runtime(Path(temporary), setup.profile_manifest(source), source, setup.Lifecycle())

    def test_private_uv_download_checks_pinned_hash_and_extracts_only_binary(self):
        for unsafe, bad_hash in ((False, False), (False, True), (True, False)):
            with self.subTest(unsafe=unsafe, bad_hash=bad_hash), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = self.archive(root, unsafe=unsafe)
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                def download(url, destination, lifecycle):
                    self.assertEqual(url, f"https://github.com/astral-sh/uv/releases/download/{setup.UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz")
                    destination.write_bytes(archive.read_bytes())
                with patch.object(setup, "find_uv", return_value=None), patch.object(setup.os, "uname", return_value=type("Uname", (), {"machine": "x86_64"})()), patch.dict(setup.UV_HASHES, {"x86_64": "0" * 64 if bad_hash else digest}), patch.object(setup, "download_uv", side_effect=download):
                    if unsafe or bad_hash:
                        with self.assertRaises(setup.SetupError):
                            setup.uv_executable(root, setup.Lifecycle())
                        self.assertFalse(any((root / "tools").glob("uv-*")))
                    else:
                        executable = Path(setup.uv_executable(root, setup.Lifecycle()))
                        self.assertTrue(os.access(executable, os.X_OK))
                        self.assertEqual(subprocess.check_output([str(executable)]), b"fixture uv\n")
                        with patch.object(setup, "download_uv", side_effect=AssertionError("reuse must not download")):
                            self.assertEqual(setup.uv_executable(root, setup.Lifecycle()), str(executable))


if __name__ == "__main__":
    unittest.main()
