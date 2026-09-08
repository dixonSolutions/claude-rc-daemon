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
claude-rc-daemon --status            # running / untrusted / missing / stray, per folder
claude-rc-daemon --trust             # only needed when auto_trust = false
journalctl --user -u claude-rc-daemon -f
ls ~/.local/state/claude-rc-daemon/logs/   # each server's terminal output
tmux attach -t rc-<folder>           # the server's own screen (QR code, session link)
```

## Config

`~/.config/claude-rc-daemon/config.toml`, see [config.example.toml](config.example.toml).

```toml
hot_paths = ["~/Projects", "~/Projects/ZuraSolutions"]
exclude   = ["SideProjects"]
claude_args = ["--no-sandbox"]
auto_trust = true
```

A hot path is never treated as a project itself, which is how a nested container like
`~/Projects/ZuraSolutions` gets its children served without being served as one.

## Per-project settings template

[settings.local.example.json](settings.local.example.json) is a Claude Code
`.claude/settings.local.json` that allows terminal, file and web-fetch tools without prompting.
Copy it into a project when you want that project to run hands-off:

```bash
cp ~/.config/claude-rc-daemon/settings.local.json <project>/.claude/settings.local.json
```

Only copy it where the file does not already exist; an existing file holds that project's own
choices. See issue #1 for making this a daemon command.

## Files

| Path | Purpose |
|---|---|
| `claude-rc-daemon` | the daemon, single file |
| `config.example.toml` | annotated config |
| `settings.local.example.json` | per-project Claude Code permissions template |
| `claude-rc-daemon.service` | systemd user unit |
| `install.sh` | link, copy, enable |
