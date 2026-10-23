# rerun-remote specification

## Outcome

Open a saved recording or attach to an existing live Rerun stream in the native desktop viewer with one command. Use existing OpenSSH aliases for remote access, keep remote files on their host, and require no application configuration file.

## Command contract

```sh
rerun-remote ./recording.rrd
rerun-remote recording-server:/absolute/path/recording.rrd
rerun-remote rerun+http://127.0.0.1:9876/proxy
rerun-remote recording-server:rerun+http://127.0.0.1:9876/proxy
rerun-remote --port 38887 recording-server:/absolute/path/recording.rrd
```

- One positional target replaces the public `view` subcommand. Remove `--host`, `--config`, TOML loading and configured relative remote paths. OpenSSH owns aliases, authentication, users, SSH ports and jump hosts. Remote recording paths must be absolute; spaces and shell metacharacters remain literal.
- A URL is an existing live endpoint; an SSH-prefixed URL is evaluated on that host. Initially support loopback HTTP Rerun proxy endpoints only, with validated ports, no credentials and no fragments. Reject malformed or unsupported targets before starting children.
- `--port` selects the local SSH forwarding port, default 38886. For remote files it also selects the temporary remote server port. For live attachment the URL specifies the existing producer port. Validate 1–65535; fail on collisions without stopping their owner or silently choosing another port. Reject explicit tunnel options for local targets.
- Preserve optional `--time TIMELINE=INTEGER_NS`: exactly one loaded recording, temporal timeline, signed integer nanoseconds excluding NaT, recorded-range validation, one paused initialization and exact readback. Preserve saved layouts, hidden panels and later user interaction. Live initialization may wait for data only within its existing deadline; it must not repeatedly seek.
- Retain remote-file resource defaults: two effective CPUs, 4 GiB memory, 128 tasks and two hours. Optional positive-integer CLI overrides replace the resources table. Reject explicit remote-file-only options for other modes.

## Runtime and ownership

- Local files go directly to the bundled viewer. Remote files use an on-demand loopback catalog server, exact recording selection and SSH forwarding. Require exactly one remote recording and at most one saved blueprint; verify the saved default blueprint through fresh readback.
- Local live attachment launches a viewer only. Remote live attachment launches a tunnel and viewer only: it must not require rerun-remote, DataFusion or systemd on the producer host. Use an existing compatible Rerun endpoint, never launch the recording application.
- The CLI owns only processes it starts. Viewer closure, interruption, terminal termination, startup failure and SSH loss clean up owned children. Existing live producers, services and listeners survive. Do not promise automatic producer restart, reconnection or complete live history.
- Continue verifying effective systemd/cgroup memory, PID, runtime and CPU-affinity controls before remote file registration. Keep listeners on loopback. A live attachment must not apply the file server's limits to its producer.
- Bound startup and external calls. Distinguish tunnel establishment from an available upstream endpoint. Report useful failures and a bounded native-server diagnostic excerpt without corrupting machine-readable readiness output.

## Installation and compatibility

- Upgrade the tested dependency profile to rerun-sdk 0.40.0 and compatible catalog dependencies. Keep exact tested pins and clear SDK/native/server mismatch errors; verify Linux and macOS executable discovery.
- Persistent installation and updates are explicit uv operations, one documented command per required machine. Normal viewing never installs dependencies. Local viewing and remote live attachment need only the client installation; remote file serving additionally needs the server extra.
- Resolve the remote executable from noninteractive SSH PATH, then the standard `$HOME/.local/bin/rerun-remote` installation. Use fixed, safely quoted resolution code without sourcing shell profiles. Diagnose a missing executable with the explicit server installation remedy; custom executable directories must be on the SSH PATH.
- Keep README focused on installation, the four viewing forms, essential prerequisites and limitations. Remove the version banner, configuration instructions, optional reference material, stale test counts and investigation history. Keep precise compatibility and acceptance details here or in package metadata.

## Verification contract

Use a bounded synthetic producer created by this work; no dependency on another owner's service or private recording is required. It must expose a loopback Rerun stream with a stable recording identity and samples that continue advancing, and produce a small saved recording with a known blueprint. Use an isolated local SSH fixture when no dedicated test host is necessary. Never alter normal SSH configuration or an existing recorder.

| Area | Required evidence |
| --- | --- |
| Public interface | Installed-wheel invocations cover all four target types, custom port and rejection of removed/configuration forms, relative remote paths, invalid ports/URLs and irrelevant options. |
| Saved files | Native local and SSH viewing load the selected recording and saved blueprint; no-blueprint input works; `--time` reads back exact paused nanoseconds and preserves saved panel state. Cover an older recording when an independent fixture is available. |
| Local live | Actual producer and native viewer show samples/timeline advancing after attachment. Closing or interrupting the CLI leaves the producer alive and able to serve a second viewer. |
| SSH live | The same checks cross real SSH forwarding, including a producer port different from the forwarding port. The path works without the remote package/server extra and leaves the producer alive after closure, interruption and failed startup. |
| Failure and ownership | Unavailable endpoints, port collisions, malformed readiness, version/dependency mismatch, native startup failure, SSH loss and signals produce bounded failure/cleanup while unrelated processes/listeners remain intact. Native failure messages reach the user. |
| Resource protection | Remote file serving verifies actual limits before registration; unavailable controls fail closed. A changed port reaches listener checks, server launch, catalog URLs, forwarding and readiness validation. |
| Installation | Build and install the wheel in an isolated environment; invoke help and representative behavior from outside the source tree. Repeated viewing uses the same installation. |

Keep focused automated regressions for inputs, quoting, process ownership, exact time, blueprint selection, live attachment and failure propagation. Native/SSH checks must observe actual viewer state or advancing data, not merely command construction. Record reproducible commands and actual results in the PR; distinguish Linux evidence, macOS regression coverage, user desktop checks and full-recording gaps. A distinct independent reviewer must review the final diff and acceptance evidence before the PR is ready.

Run focused checks with `python -m unittest discover -s tests -p test_cli.py`. Build and install the wheel, then run `RERUN_REMOTE_NATIVE_TESTS=1 python tests/test_native.py -v` on Linux with Xvfb, OpenSSH client/server and a working systemd user manager. The suite creates its own producer and isolated SSH configuration. Set `RERUN_REMOTE_NATIVE_ARTIFACTS` to retain screenshots for panel-state review. The cross-host case additionally needs `RERUN_REMOTE_TEST_HOST` and `RERUN_REMOTE_TEST_DIRECTORY` pointing to an explicitly authorized disposable server installation; `RERUN_REMOTE_TEST_SERVER_BIN` supplies its temporary executable directory when needed. Remove the remote setup after verification.

## Delivery boundary

This change includes the specification, implementation, tests, lean documentation, a reviewed feature branch and a pull request. Preserve the existing checkout and foreign work. Do not merge, publish a package, choose a license, change remote production hosts or install an always-running service. A standalone bootstrap installer, growing-file following, recording/application launch, browser UI, discovery catalogs and automatic reconnection remain later work.
