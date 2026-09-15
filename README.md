# claude-rc-daemon

One `claude remote-control` server per project folder, kept in sync with what is on disk.

## What it does

[Claude Code Remote Control](https://code.claude.com/docs/en/remote-control) pins a server to the
folder it was started in, so one server cannot serve your other projects. This daemon watches a
few *hot paths* (for example `~/Projects`) and, for every git repository directly inside them,
keeps a server running in a tmux session named `rc-<folder>`.

- Folder appears: a server starts (after a short settle so `git clone` can finish).
- Folder disappears: its server is stopped.
- Loose files in a hot path are ignored. Only directory events are watched, non-recursively.
- A folder that merely *holds* projects (no `.git` of its own) is skipped with a hint.
  Add it as a hot path if you want its children served.
- A server already running in a project folder is adopted, never duplicated.
- Servers keep running if the daemon stops. They live in tmux, not in the daemon.
- A server that is alive but stuck reconnecting is restarted. A dropped connection does not kill
  `claude remote-control` -- it retries in-process forever, so the PID stays healthy while
  claude.ai/code lists the device as **offline**. The daemon reads each server's own status line
  and restarts anything that has been disconnected for `max_disconnected_seconds` (default 300).

## Surviving logouts, outages and reboots

A server exits when Claude Code is logged out ("You must be logged in to use Remote Control") or
when it cannot reach Anthropic for ~10 minutes ("Server unreachable ... giving up"). The daemon
tells these apart from crashes by reading the server's log:

- **Logged out**: every start is held. It checks `claude auth status` again whenever
  `~/.claude/.credentials.json` changes, so the servers come back seconds after you log in.
- **Network down**: starts are held while `api.anthropic.com:443` is unreachable. It re-checks
  every 10 s and restarts everything once the connection works again.
- **Crash**: normal exponential back-off, capped at `max_backoff_seconds` (default 10 min).
  Recovering from a logout or an outage also clears this back-off.
- **Reboot**: linger starts the daemon at boot, and starts wait until the network is up.
- **Daemon dies**: systemd restarts it (`Restart=always`, no start limit). The servers keep running
  in tmux and are adopted again.

A restarted server reconnects the sessions it had before. Claude Code keeps the environment
("Environment preserved. Restart `claude remote-control` to reconnect existing sessions").

## Stack

- Python 3.11+ standard library only: `tomllib`, `ctypes` for inotify, `subprocess` for tmux.
- tmux for detached, attachable sessions that outlive the daemon.
- systemd user unit for lifetime management. Logs go to the journal.
- No third-party packages, no database, no network of its own.

## Setup

```bash
git clone git@github.com:dixonSolutions/claude-rc-daemon.git ~/Projects/claude-rc-daemon
~/Projects/claude-rc-daemon/install.sh
```

`install.sh` links the script into `~/.local/bin`, copies the example config and settings
template to `~/.config/claude-rc-daemon/` if they are not there yet, installs and enables the
user unit, turns on linger, and accepts workspace trust for every project it finds.

Claude Code refuses to serve a folder whose trust dialog was never accepted, and trust does not
inherit from a parent folder. With `auto_trust = true` (the default in the example config) the
daemon records trust for any new project folder it discovers under a hot path, so a fresh
`git clone` is served within a minute with no further steps. Set it to `false` if you want to
approve folders yourself with `claude-rc-daemon --trust`, which lists them and asks first.

## Daily use

```bash
claude-rc-daemon --status            # running / stalled / untrusted / missing / stray, per folder
                                     # LINK column is the live connection: connected / disconnected
claude-rc-daemon --trust             # only needed when auto_trust = false
journalctl --user -u claude-rc-daemon -f
ls ~/.local/state/claude-rc-daemon/logs/   # each server's terminal output
tmux attach -t rc-<folder>           # the server's own screen (QR code, session link)
```

## Config

`~/.config/claude-rc-daemon/config.toml`, see [config.example.toml](config.example.toml).

```toml
hot_paths = ["~/Projects"]          # the default when omitted
exclude   = []
claude_args = ["--no-sandbox"]
auto_trust = true
capacity = 32                       # max concurrent sessions per project (--capacity)
permission_mode = "auto"            # for spawned sessions (--permission-mode); "" = project settings
spawn_mode = "worktree"             # worktree | same-dir | session
remote_all_sessions = true          # every interactive `claude` starts with Remote Control on
convert_sessions = true             # also switch on already-open sessions (tmux only)
```

## Every Claude session remoted

Project servers cover claude.ai/code. Sessions you open yourself in a terminal are covered too:

- `remote_all_sessions = true` keeps `remoteControlAtStartup: true` in `~/.claude/settings.json`,
  so every new interactive `claude` starts with Remote Control on. It is re-applied on every
  reconcile, so a reset doesn't last. A resumed session reconnects to its remote session.
  `false` forces the setting off; leaving the option out of the config leaves the setting alone.
- `convert_sessions = true` switches on sessions that were already open without Remote Control.
  Claude Code has no external switch for this, so the daemon types `/remote-control` into the
  session's tmux pane. It only does so when the session is idle and nothing is typed in the prompt,
  and waits otherwise. Sessions outside tmux can't be switched from outside, because terminal input
  injection (TIOCSTI) is disabled on current kernels. The daemon names them in the journal once.

Changing `capacity`, `permission_mode`, `spawn_mode` or `claude_args` takes effect without a manual restart. Idle
servers whose command line no longer matches the config are restarted on the next reconcile. A
server with sessions attached waits until they end (`restart_on_config_change`).

A hot path is never treated as a project itself. If a folder inside `~/Projects` only holds
projects, add it too, e.g. `hot_paths = ["~/Projects", "~/Projects/Work"]`, and its children are
served while it is not.

## Per-project settings template

[settings.local.example.json](settings.local.example.json) is a Claude Code
`.claude/settings.local.json` that allows terminal, file and web-fetch tools without prompting.
`install.sh` copies it to `~/.config/claude-rc-daemon/settings.local.json` (`settings_template`).
The daemon seeds that file into every served project that has no `.claude/settings.local.json`
yet and adds it to the project's `.git/info/exclude`. An existing file holds that project's own
choices and is never overwritten. Add `"defaultMode": "auto"` under `permissions` in the template
to make new sessions start in that mode, or set `permission_mode` in the config to force it for
every project. Set `settings_template = ""` to turn seeding off.

## Files

| Path | Purpose |
|---|---|
| `claude_rc_daemon.py` | the daemon, single Python file (linked to `~/.local/bin/claude-rc-daemon`) |
| `config.example.toml` | annotated config |
| `settings.local.example.json` | per-project Claude Code permissions template |
| `claude-rc-daemon.service` | systemd user unit |
| `install.sh` | link, copy, enable |
