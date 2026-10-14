# rerun-remote: desktop recording tool

## Goal

Install a small tool once on a laptop and execution server, then open a local or remote RRD in the desktop Rerun viewer with one laptop command. Remote source recordings remain on the server; viewing transfers measurement data over SSH without copying the source file to the laptop.

## User commands

```sh
rerun-remote view ./recording.rrd
rerun-remote view recording-server:/path/recording.rrd
rerun-remote view --host recording-server recording.rrd
```

The third form resolves a relative recording path under that host's configured recording directory. Hosts use existing OpenSSH aliases and authentication. Paths containing spaces must work. These commands are the supported interface. Optional `--time TIMELINE=INTEGER_NS` selects an explicit startup cursor on a duration or timestamp timeline; the integer is nanoseconds, not seconds or sequence ticks.

## Requirements

1. Use the existing desktop Rerun viewer for visualization. Open the selected recording automatically, including its saved blueprint when present. Local viewing uses the existing file directly.
2. For remote viewing, handle server startup, exact recording selection, SSH forwarding and launching the desktop viewer internally. The user does not copy connection URLs, dataset IDs or tunnel commands. Preserve the currently agreed default port 38886; report a collision without stopping its owner or silently selecting another port.
3. Package a small standalone Python CLI installable with uv on the laptop and server. Installation and upgrades are explicit operations; normal viewing does not reinstall dependencies. Keep the client/viewer/server versions compatible, using Rerun 0.39.0 and compatible catalog dependencies. Support persistent installation; preserve explicitly retained older installations and package artifacts when staging an update. Detect a mismatch and report a useful remedy.
4. Keep host aliases, recording directories and execution paths in user configuration or explicit arguments. The tool must work as a standalone package, without hard-coded private recording paths or dependencies on a project-specific runtime.
5. Start viewing sessions on demand. Closing the viewer or interrupting the command retires the session's owned SSH/server children. Failed startup and lost SSH connections produce a useful error and clean up owned processes. Existing user services, recordings and unrelated environments remain intact. Persistent installation and configuration do not require an always-running daemon.
6. Preserve ordinary SSH authentication and loopback-only listeners. Quote remote paths correctly. Respect execution-host resource limits using the existing native mechanisms; do not create a scheduler or policy framework.

7. When `--time` is supplied, use the supported ViewerClient runtime controls after bounded observable readiness to pause and initialize exactly the selected owned recording once at the requested integer nanoseconds. Preserve saved layout and hidden panels and subsequent user camera/cursor interactions. Reject malformed, unavailable or non-temporal timeline/time selection clearly. Without this argument, retain normal native behavior. Saved panel/layout state belongs to the recording; do not add a project-specific metadata-hint schema or continuously enforce a cursor.

## Completion checks

- From an installed package, one command opens a local saved RRD in desktop Rerun with its saved layout and time controls.
- One laptop command opens an explicitly selected remote RRD through SSH, with its saved layout and forward/backward seeking, without copying the RRD to the laptop or requiring a manual link/tunnel step. Use an existing bounded recording for the first actual native smoke, then provide the complete-recording command for the user's laptop trial. Distinguish observed native-client behavior from laptop behavior that cannot be exercised here.
- Actual bounded installed 0.39.0 checks cover an existing 0.33.1 RRD, explicit exact startup time/pause, saved hidden-panel/layout preservation, default/no-blueprint viewing and time-selection errors. Distinguish execution-host native evidence from intended-laptop and full-recording behavior.
- Repeat invocation uses the existing installation. Focused consequence checks cover paths with spaces/shell metacharacters, wrong versions, missing input, occupied resources, startup failure, connection loss and owned cleanup. Avoid a broad platform matrix or tests that only reproduce command construction.
- The integrated result receives the requested independent review. README contains exact installation, update and view commands, supported platforms, and any observed limitations. Return the working commands and remaining gaps promptly.

## First-delivery boundary

Desktop viewing, installation/update instructions, minimal host configuration, session cleanup, Rerun 0.39.0 compatibility and optional startup time are in scope. Browser hosting, custom visualization UI, recording/conversion, mapping, catalogs for discovery, multi-user services, schedulers, daemon managers and a new evidence/report framework are deferred. Initial private GitHub source publication is authorized. Public releases, PyPI publication and a source-license selection remain outside this private stage. Material scope changes require an explicit requirements amendment.
