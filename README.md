# rerun-remote

Open saved recordings and live Rerun streams in the native desktop viewer. Remote access uses your existing OpenSSH aliases; remote recording files stay on their host. No application configuration file is needed.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run on your laptop:

```sh
uv tool install --python 3.12 'rerun-remote @ git+https://github.com/genzorr/rerun-remote.git@main'
```

Your laptop needs Git. Installation uses HTTPS and requires no GitHub account or SSH key. Ensure uv's executable directory is on your local PATH. Remote viewing uses your existing SSH aliases and authentication; no separate rerun-remote installation or GitHub credentials are needed on the recording server.

To update, repeat the install command with `--reinstall --upgrade`.

## Open a recording or stream

```sh
# Saved recordings
rerun-remote ./recording.rrd
rerun-remote recording-server:/srv/recordings/recording.rrd

# Existing live Rerun endpoints
rerun-remote rerun+http://127.0.0.1:9876/proxy
rerun-remote recording-server:rerun+http://127.0.0.1:9876/proxy
rerun-remote --recording my-app my-session recording-server:rerun+http://127.0.0.1:9876/proxy
```

Saved recordings start with Blueprint and Selection collapsed. Timeline starts collapsed for recordings with time data and stays hidden for static files. You can change the panels from the viewer.

With the [patched macOS Viewer](native/README.md) installed, keep the remote-file window open and press **⌘⇧L** to open another `recording-server:/absolute/path/file.rrd`. Rerun opens it in the same window. The session reuses the connection for that host and chooses ports for new hosts automatically.

Use an existing SSH alias and an absolute remote file path. Quote targets containing spaces. Live targets must be loopback Rerun HTTP proxy endpoints; start the compatible producer separately.

On a shared live endpoint, pass `--recording APPLICATION_ID RECORDING_ID` to show only that recording and keep receiving its updates. Use the IDs reported by the producer or from the recording's details in an existing Viewer. Omit the option to see all available recordings. The selection happens on your laptop, so SSH still carries the shared stream. If matching data is unavailable, the command reports both requested IDs and exits.

The first remote file opening prepares a matching server runtime in your account's cache. The package travels over SSH; the server downloads public dependencies and a private Python runtime as needed. Setup takes longer the first time and reports progress. Later openings reuse the cache; client updates get a separate matching environment. Existing installations and running sessions are preserved. Local files and live connections do not trigger remote setup.

Keep the command running while viewing. Closing the viewer or interrupting the command cleans up its viewer, SSH tunnel and temporary file server. Existing live producers keep running. If an in-app connection drops, the Viewer stays open. Open another path for that host to reconnect. For other SSH sessions, the Viewer closes when the connection drops.

With the stock Viewer and for live streams, SSH forwarding uses port **38886** by default. In-app sessions choose an available port automatically. Use `--port 38887` to choose another local forwarding port; for remote files this also changes the temporary server port. For live streams, the URL specifies the producer port. Occupied ports fail without disturbing their owner.

See `rerun-remote --help` for exact startup-time selection and remote-file resource overrides.

## Requirements and limits

- Desktop clients support macOS and Linux with a working graphics backend. Windows is unsupported.
- Remote file serving requires Linux, Python 3.10 or later for the bootstrap, an existing systemd user manager, cgroup v2 memory/PID delegation and `taskset`. First-use setup needs HTTPS access to public dependency sources. Default limits are two CPUs, 4 GiB memory, 128 tasks and two hours; unavailable controls stop startup before dependency installation.
- Remote files must contain one recording and at most one saved blueprint. Live history depends on the producer's buffer and is separate from saving a recording.
- Server compatibility is managed automatically. Dependency pins live in [pyproject.toml](pyproject.toml); behavior and verification requirements live in [REQUIREMENTS.md](REQUIREMENTS.md).

Licensed under [MIT](LICENSE).
