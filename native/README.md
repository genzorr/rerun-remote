# Build the macOS Viewer with in-app SSH opening

The launcher uses an Apple silicon Rerun 0.43.2 bundle with the [Open from URL patch](rerun-0.43.2-ssh-open.patch) for remote saved-file sessions. The SDK and server still come from the matching upstream wheel. The patch forwards SSH paths from the existing dialog to the launcher through a private session socket.

On an Apple silicon Mac with Xcode Command Line Tools, Rust 1.98 and `protoc` on PATH or set through `PROTOC`, prepare a separate checkout and build the native executable.

```sh
git clone --depth 1 --branch 0.43.2 https://github.com/rerun-io/rerun.git rerun-0.43.2
git -C rerun-0.43.2 apply /absolute/path/to/rerun-remote/native/rerun-0.43.2-ssh-open.patch
cd rerun-0.43.2
cargo +1.98.0 build --locked --release -p rerun-cli --no-default-features --features base,lance --target aarch64-apple-darwin
```

Put the executable into `Contents/MacOS/Rerun` in an app bundle copied from the matching SDK, sign the bundle locally, and verify its version and the patched dialog with a synthetic remote recording. Install the bundle under `~/.local/share/rerun-remote/viewer/0.43.2/Rerun.app` with a sibling `remote-open-v1` file containing the executable’s SHA-256 once the patched Viewer passes those checks. Install the matching launcher from this checkout. To test, launch `rerun-remote recording-server:/data/first.rrd`, then use **⌘⇧L** to open `recording-server:/data/next.rrd` and confirm the window stays in place.
