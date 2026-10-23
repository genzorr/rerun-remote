# rerun-remote

Open saved recordings and live Rerun streams in the native desktop viewer. Remote access uses your existing OpenSSH aliases; remote recording files stay on their host. No application configuration file is needed.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run on your laptop:

```sh
uv tool install --python 3.12 'rerun-remote @ git+ssh://git@github.com/genzorr/rerun-remote.git'
```

Your laptop needs Git and access to this private repository through GitHub SSH. Ensure uv's executable directory is on your local PATH. No separate rerun-remote installation or GitHub credentials are needed on the recording server.

To update, repeat the install command with `--reinstall --upgrade`.

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

The first remote file opening prepares a matching server runtime in your account's cache. The package travels over SSH; the server downloads public dependencies and a private Python runtime as needed. Setup takes longer the first time and reports progress. Later openings reuse the cache; client updates get a separate matching environment. Existing installations and running sessions are preserved. Local files and live connections do not trigger remote setup.

Keep the command running while viewing. Closing the viewer or interrupting the command cleans up its viewer, SSH tunnel and temporary file server. Existing live producers keep running. SSH loss ends the owned viewing session; reconnection is manual.

SSH forwarding uses port **38886** by default. Use `--port 38887` to choose another local forwarding port; for remote files this also changes the temporary server port. For live streams, the URL specifies the producer port. Occupied ports fail without disturbing their owner.

See `rerun-remote --help` for exact startup-time selection and remote-file resource overrides.

## Requirements and limits

- Desktop clients support macOS and Linux with a working graphics backend. Windows is unsupported.
- Remote file serving requires Linux, Python 3.10 or later for the bootstrap, an existing systemd user manager, cgroup v2 memory/PID delegation and `taskset`. First-use setup needs HTTPS access to public dependency sources. Default limits are two CPUs, 4 GiB memory, 128 tasks and two hours; unavailable controls stop startup before dependency installation.
- Remote files must contain one recording and at most one saved blueprint. Live history depends on the producer's buffer and is separate from saving a recording.
- Server compatibility is managed automatically. Dependency pins live in [pyproject.toml](pyproject.toml); behavior and verification requirements live in [REQUIREMENTS.md](REQUIREMENTS.md).

Licensed under [MIT](LICENSE).
