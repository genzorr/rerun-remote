# rerun-remote

Open saved recordings and live Rerun streams in the native desktop viewer. Remote access uses your existing OpenSSH aliases; remote recording files stay on their host. No application configuration file is needed.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run on your laptop:

```sh
uv tool install --python 3.12 'rerun-remote @ git+ssh://git@github.com/genzorr/rerun-remote.git'
```

For remote **files**, also install on the recording server:

```sh
uv tool install --python 3.12 'rerun-remote[server] @ git+ssh://git@github.com/genzorr/rerun-remote.git'
```

Each installing machine needs Git and access to this private repository through GitHub SSH. Ensure uv's executable directory is on your local PATH. Remote file serving also checks the standard `~/.local/bin/rerun-remote` location; a custom installation directory must be on the noninteractive SSH PATH. Attaching to an existing remote live stream needs only the laptop installation and SSH access.

To update, repeat the appropriate install command with `--reinstall --upgrade`. Viewing never installs or updates dependencies.

## Open a recording or stream

```sh
# Saved recordings
rerun-remote ./recording.rrd
rerun-remote recording-server:/srv/recordings/recording.rrd

# Existing live Rerun endpoints
rerun-remote rerun+http://127.0.0.1:9876/proxy
rerun-remote recording-server:rerun+http://127.0.0.1:9876/proxy
```

Use an existing SSH alias and an absolute remote file path. Quote targets containing spaces. Live targets must be loopback Rerun HTTP proxy endpoints; start the compatible producer separately.

Keep the command running while viewing. Closing the viewer or interrupting the command cleans up its viewer, SSH tunnel and temporary file server. Existing live producers keep running. SSH loss ends the owned viewing session; reconnection is manual.

SSH forwarding uses port **38886** by default. Use `--port 38887` to choose another local forwarding port; for remote files this also changes the temporary server port. For live streams, the URL specifies the producer port. Occupied ports fail without disturbing their owner.

See `rerun-remote --help` for exact startup-time selection and remote-file resource overrides.

## Requirements and limits

- Desktop clients support macOS and Linux with a working graphics backend. Windows is unsupported.
- Remote file serving requires Linux, an existing systemd user manager, cgroup v2 memory/PID delegation and `taskset`. Default limits are two CPUs, 4 GiB memory, 128 tasks and two hours; unavailable controls stop startup.
- Remote files must contain one recording and at most one saved blueprint. Live history depends on the producer's buffer and is separate from saving a recording.
- Install matching client/server Rerun profiles. Dependency pins live in [pyproject.toml](pyproject.toml); behavior and verification requirements live in [REQUIREMENTS.md](REQUIREMENTS.md).
