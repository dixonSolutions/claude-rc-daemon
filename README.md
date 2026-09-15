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
- **Crash**: exponential back-off per folder, capped at `max_backoff_seconds` (default 5 min), so
  one broken project never delays the others. After `failing_after` (10) crashes in a row the folder
  is marked **failing**: one error in the journal with its last output, `failing` in `--status`, and
  retries drop to hourly. Recovering from a logout or an outage clears all back-off.
- **Environment refusal**: a server that exits because of its environment (`DISABLE_GROWTHBOOK`
  set, API-key auth instead of claude.ai, a long-lived token without full scope) holds every start
  until the daemon restarts, since retrying cannot help. Variables known to cause this are named in
  the journal at startup and in `--status`.
- **Stuck on a prompt**: a server waiting on the one-time "Enable Remote Control? (y/n)" question
  is answered yes.
- **Account switch**: a server stays registered under the account it started with, and only a Claude
  app signed in to that same account lists it. When `claude auth login` signs in to a different
  account, the daemon restarts every server so they register under the new one. That also works if
  the switch happened while the daemon was down. `--status` shows the account (`login: yes as …`).
- **Reboot**: linger starts the daemon at boot, and starts wait until the network is up.
- **Daemon dies**: systemd restarts it (`Restart=always`, no start limit). The servers keep running
  in tmux and are adopted again.

A restarted server reconnects the sessions it had before. Claude Code keeps the environment
("Environment preserved. Restart `claude remote-control` to reconnect existing sessions").
`claude remote-control --continue` is not used: Claude Code rejects it together with `--spawn` and
`--capacity`, which every server here needs.

`--status` shows why a folder is down: the last exit reason (`auth`, `env`, `network`, `crash`)
with the line the server printed, the retry countdown, and any hold on the whole daemon.

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
claude-rc-daemon --status            # running / stalled / prompt / failing / backoff / untrusted / missing / stray
                                     # LINK column is the live connection: connected / disconnected
claude-rc-daemon --trust             # only needed when auto_trust = false
claude-rc-daemon --seed-settings     # copy the settings template where missing (add --dry-run to preview)
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

## The machine remote

A hot path such as `~/Projects` is never served as a project, so there is no remote for the folder
itself unless you set `machine_remote = "~/Projects"`. The daemon then keeps one extra server there
under the machine's hostname (or `machine_remote_name`), with the same logout, outage and restart
handling as project servers. Plain folders use `same-dir` sessions. The home directory can't be used,
because Claude Code never saves trust for it.

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
- A session whose link has died shows `/rc failed` or "/remote-control is no longer active". If it
  runs in tmux, the daemon turns Remote Control on again the same way. Outside tmux it can't, so start
  long-lived sessions in tmux (`tmux new -s work`, then `claude`).

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
every project. Set `settings_template = ""` to turn seeding off. `claude-rc-daemon --seed-settings`
does the same on demand and prints how many files it created and skipped.

## Tests

```bash
python3 -m unittest discover -s tests
```

Stall detection and session conversion read text that Claude Code paints for people, which can
change in any release. `tests/fixtures` holds screens recorded from Claude Code 2.1.271–2.1.272, so a wording
change fails a test once the fixtures are refreshed. In the field, the daemon also logs a warning when
no server shows a status line it recognises.

## Files

| Path | Purpose |
|---|---|
| `claude_rc_daemon.py` | the daemon, single Python file (linked to `~/.local/bin/claude-rc-daemon`) |
| `config.example.toml` | annotated config |
| `settings.local.example.json` | per-project Claude Code permissions template |
| `claude-rc-daemon.service` | systemd user unit |
| `install.sh` | link, copy, enable |
| `tests/` | unit tests and recorded Claude Code screens |
