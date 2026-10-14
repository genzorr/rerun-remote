# rerun-remote

Open a local or SSH-hosted `.rrd` recording in the native Rerun desktop viewer with one command. Remote recordings stay on the server; measurements travel through SSH without copying the source file to the laptop. The tool selects the recording and its saved blueprint and cleans up its owned processes when viewing ends.

Version **0.2.1** uses **Rerun 0.39.0** and server **DataFusion 54.0.0**. This client fixes macOS app-bundle executable discovery and remains compatible with an existing 0.2.0 server using that same profile; that server needs no reinstall for this client fix.

## Install and update

Clients support **macOS and Linux** with a working desktop graphics backend. Serving recordings requires **Linux**, an existing systemd user manager, cgroup v2 memory/PID delegation and `taskset`. Installations need [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, Python 3.11+ and OpenSSH. The commands below request Python 3.12, which was tested.

This is a private GitHub repository. Each installing machine needs existing GitHub SSH authentication with access to `genzorr/rerun-remote`; recording-server SSH authentication is separate. The source URL uses [uv's supported SSH authentication](https://docs.astral.sh/uv/concepts/authentication/git/).

On the laptop:

```sh
uv tool install --python 3.12 'rerun-remote @ git+ssh://git@github.com/genzorr/rerun-remote.git'
```

On the execution server, include the `server` extra:

```sh
uv tool install --python 3.12 'rerun-remote[server] @ git+ssh://git@github.com/genzorr/rerun-remote.git'
```

[uv installs tools](https://docs.astral.sh/uv/guides/tools/) into persistent isolated environments. The executable directory is usually `~/.local/bin`; `uv tool dir --bin` shows its actual location. If uv reports that directory is outside your shell's PATH, run `uv tool update-shell` and open a new shell. Viewing uses the existing installation and never installs dependencies.

To update explicitly from the repository's current default branch, run the matching command:

```sh
# Laptop
uv tool install --reinstall --upgrade --python 3.12 'rerun-remote @ git+ssh://git@github.com/genzorr/rerun-remote.git'
# Execution server
uv tool install --reinstall --upgrade --python 3.12 'rerun-remote[server] @ git+ssh://git@github.com/genzorr/rerun-remote.git'
```

Client, native viewer and server must use the matching Rerun profile. Update only the affected installation when a mismatch is reported.

## Configure a remote host

Use an existing OpenSSH alias, such as `recording-server`. Save optional configuration at `~/.config/rerun-remote/config.toml`:

```toml
[hosts.recording-server]
recording_dir = "/srv/recordings"
executable = "/home/YOU/.local/bin/rerun-remote"

[resources]
cpu_count = 2
memory_bytes = 4294967296
tasks_max = 128
runtime_seconds = 7200
```

Replace the example paths with your own absolute remote paths. An explicit `executable` is recommended because noninteractive SSH may have a different PATH from your login shell. Its default is `rerun-remote` in that SSH PATH. `recording_dir` is required only for relative recording paths with `--host`; the configuration contains no credentials.

The resource values above are the remote server defaults: two effective CPUs, 4 GiB memory, 128 tasks and two hours. The server verifies its systemd/cgroup limits and inherited CPU affinity before recording registration; unavailable controls stop startup. No always-running daemon or host setup is installed.

To use another configuration file:

```sh
rerun-remote --config /path/config.toml view --host recording-server recording.rrd
```

## View

Start with ordinary viewing:

```sh
rerun-remote view ./recording.rrd
rerun-remote view recording-server:/srv/recordings/recording.rrd
rerun-remote view --host recording-server recording.rrd
```

The third form resolves the relative file under the configured `recording_dir`. Quote paths containing spaces or shell metacharacters:

```sh
rerun-remote view 'recording-server:/srv/recordings/recording with spaces.rrd'
```

Keep the command running while viewing. Viewer closure, Ctrl+C, SIGTERM, terminal closure (SIGHUP) and SSH loss retire the owned viewer, tunnel and remote server. Existing services and unrelated processes are preserved.

The SSH forward and remote server bind only to loopback port **38886**. An occupied port causes an error; the tool preserves its owner and never silently chooses another forwarding/server port. Remote files must contain exactly one recording and at most one saved blueprint. Local files are passed directly to Rerun.

## Optional startup time

Ordinary viewing retains normal native startup behavior. Add `--time TIMELINE=INTEGER_NS` only when you want to initialize an exact recorded time, paused:

```sh
rerun-remote view ./recording.rrd --time elapsed=1500000000
rerun-remote view recording-server:/srv/recordings/recording.rrd --time capture_time=1700000000123456789
```

Use a timeline name and time that exist in your recording; these examples are not defaults. Values are signed 64-bit integer **nanoseconds** on a **duration or timestamp** timeline, within its recorded range. Sequence timelines, fractional values and the reserved NaT value are rejected. This option requires exactly one loaded recording.

The tool verifies ownership of an internal loopback control listener, waits for the selected recording to load, sets its paused cursor once and confirms exact readback. Startup initialization is bounded to 120 seconds and stops if the desktop or SSH connection exits. It fails safely if listener ownership cannot be observed.

Saved layout and Blueprint/Selection/Time panel visibility belong to the recording. Startup time initialization does not change them or continually reset later cursor/camera interactions. Cursor readiness can precede lazy measurement arrival; let the views finish loading before judging an initially empty image or geometry view.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Private Git installation cannot authenticate | Check that this machine's existing GitHub SSH key has access to the repository. |
| SSH startup fails or the remote command is missing | Check the recording-server alias/authentication and configure its absolute `executable` path. |
| SDK/native/server version mismatch, or missing bundled executable | Run the explicit update command for the affected client/server. The current client handles Rerun 0.39's macOS app bundle. |
| Missing server catalog dependencies | Install or update using `rerun-remote[server]`; the server requires DataFusion 54.0.0. |
| Resource controls cannot be verified | Use a Linux host with an existing systemd user manager, cgroup v2 memory/PID delegation and `taskset`. Startup fails closed. |
| Port 38886 is occupied | Close the session that you intend to stop, then retry. The tool does not stop that port's owner. |
| Startup time is rejected | Check the timeline name/type, integer nanoseconds and recorded range, or omit `--time` for ordinary viewing. |
| SSH disconnects during viewing | Owned processes are retired; inspect the preceding SSH/server diagnostic before retrying. |

## Verification and limits

The current client passed 45 bounded CLI checks and five executable/version regressions importing the built wheel. Linux native checks exercised local/SSH viewing, saved blueprints and exact paused startup time. macOS 0.2.1 desktop success is user-reported, without independent laptop pixel verification. Full-recording behavior has not been independently verified. Windows is unsupported.
