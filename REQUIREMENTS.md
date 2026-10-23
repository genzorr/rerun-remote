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
- Verify effective systemd/cgroup memory, PID, runtime and CPU-affinity controls before provisioning remote dependencies, and again before remote file registration. Reject missing files, occupied ports and unsupported hosts before expensive setup. Keep listeners on loopback. A live attachment must not apply the file server's limits to its producer.
- Bound startup and external calls. Distinguish tunnel establishment from an available upstream endpoint. Report useful failures and a bounded native-server diagnostic excerpt without corrupting machine-readable readiness output.

## Installation and compatibility

- Install and update the laptop explicitly with uv from the public repository over HTTPS, without GitHub credentials or an SSH key. Opening a remote saved file automatically provisions its matching server runtime on first use; subsequent openings reuse it. Local files and existing local/remote live endpoints never provision anything remotely. Do not require a separately installed remote rerun-remote executable or GitHub credentials on that host.
- Transfer the small installed Python package over the existing owned SSH connection. Identify a runtime by its actual package bytes, exact dependency pins, Python profile, bootstrap format and remote platform/architecture; package version alone is insufficient, and different server architectures sharing a cache must receive separate runtimes. Keep the tested rerun-sdk 0.40.0 and compatible DataFusion profile, native executable checks and exact version validation.
- Include the MIT license notice in source distributions, installed wheels and the package transferred to each remote runtime.
- Use a private per-user cache under `$XDG_CACHE_HOME/rerun-remote` or `~/.cache/rerun-remote`. Existing installations, user configuration and completed profiles remain intact. Upgrades create a separate matching profile; an older active session keeps its own runtime. Do not install globally, use sudo, edit shell profiles or start a permanent service.
- A small standard-library bootstrap may use the host's existing Python 3.10 or later. Prepare the application runtime with uv-managed Python 3.12 when necessary. Find an existing uv through PATH or its standard user location; otherwise obtain a pinned official standalone uv artifact, verify its checksum and install it only in the private cache. Public dependency downloads require HTTPS access; recording-host authentication remains standard SSH.
- Provision only inside verified resource controls. Serialize installation for the same profile with a cancellable bounded lock, stage each attempt separately, validate it and publish atomically. Never unlink a shared lock or delete another attempt, completed profile or active runtime. A failed/interrupted attempt leaves no usable partial profile; retry succeeds with fresh staging. Invoke the final Python runtime directly rather than relying on relocated console-script shebangs.
- Bound and validate payload framing, size, filenames, requirements and content identity before execution/extraction. Keep stdout for readiness JSON and report concise setup/reuse progress and actionable failures on stderr. Bound download/install, payload writes, lock waiting and readiness; SSH loss/signals cancel owned setup children as well as viewing children. Warm use must work without network access to package sources.
- Keep README focused on laptop installation, the four viewing forms, first-use setup and essential prerequisites/limitations. No version banner, separate server installation recipe or configuration instructions. Keep precise compatibility and acceptance details here or in package metadata.

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
| First-use setup | From an installed client wheel, open a synthetic file on an authorized Linux test host using an empty isolated cache and no remote package/repository credentials. Observe automatic preparation followed by native recording, blueprint and exact paused time. Test the private uv/Python bootstrap path as well as reuse of existing tools. |
| Reuse and upgrade | A second opening uses the same complete runtime without a package-source network request. A changed source payload with unchanged package version gets a distinct profile. Old profiles and an unrelated installation remain intact; concurrent first uses on different ports share one complete installation. |
| Setup recovery | Interruption/SSH loss during setup or lock waiting stops owned work. Retry after an injected install failure succeeds without publishing partial state. Invalid/oversized payloads, unsafe paths and prerequisite failures are rejected. Existing processes, completed profiles and lock ownership are preserved. |
| Installation | Build and install the wheel in an isolated environment; invoke help and representative behavior from outside the source tree. Keep local-file and both live paths free of provisioning, and preserve the existing native/SSH behavior checks. |

Keep focused automated regressions for inputs, quoting, process ownership, exact time, blueprint selection, live attachment and failure propagation. Native/SSH checks must observe actual viewer state or advancing data, not merely command construction. Record reproducible commands and actual results in the PR; distinguish Linux evidence, macOS regression coverage, user desktop checks and full-recording gaps. A distinct independent reviewer must review the final diff and acceptance evidence before the PR is ready.

Run focused checks with unittest discovery. Build and install the wheel, then run opt-in native checks on Linux with Xvfb, OpenSSH client/server and a working systemd user manager. The suite creates its own producer and isolated SSH configuration; retain screenshots when reviewing panel state. Cross-host checks must use an explicitly authorized test directory and isolated cache, exercise automatic setup through the installed client, and remove all test-owned files, runtime/cache directories and processes afterward. Record exact commands and actual results in the PR without personal workstation paths or internal hostnames.

## Public-release review

Review current files, reachable history and GitHub repository metadata, issues, pull requests, comments, reviews and other exposed descriptions/assets for credentials, personal contact information, private paths and internal infrastructure references. Distinguish intentional project identity from sensitive information, enumerate unavailable surfaces, and report whether remediation belongs in the current tree, existing GitHub text or historical metadata. Keep detailed findings outside the repository when they contain the information under review. The source is MIT licensed. Redact internal operational details from GitHub descriptions while preserving verification claims; verify retained edit history separately. Do not rewrite Git history or change repository visibility without a separate user decision. Keep new documentation and PR text generic enough for future public use.

## Delivery boundary

The public repository distributes the MIT-licensed source through HTTPS installation. Preserve the existing checkout and foreign work. Only an authorized disposable test setup may be changed on a test host; preserve other processes and installations and remove the setup afterward. Package publication and Git history rewriting require separate authorization. A laptop bootstrap installer, growing-file following, recording/application launch, browser UI, discovery catalogs, download-and-open and automatic reconnection remain outside this implementation.
