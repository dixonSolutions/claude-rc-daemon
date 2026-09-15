#!/usr/bin/env python3
"""claude-rc-daemon: keep one `claude remote-control` server alive per project folder.

Config (TOML) lists *hot paths*; with none set, ~/Projects is used. Every immediate sub-folder of a hot path is a
candidate project; loose files in the hot path are ignored. A sub-folder is:

  * skipped   if it is itself a hot path (or an ancestor of one), is excluded by
              name/path, or starts with a dot;
  * a project if it contains `.git`;
  * a container (skipped, hinted once) if it has no `.git` but holds sub-folders
              that do -- e.g. ~/Projects/SideProjects. Add it as a hot path or exclude it.
  * skipped   otherwise when `require_git = true` (default).

Each project gets a detached tmux session running
`claude remote-control --name <folder> --spawn worktree <claude_args...>`.
The daemon watches hot paths with inotify (non-recursive, directory events only)
and reconciles desired vs running on every event and on a periodic timer.
Servers whose folder disappeared are stopped. Servers already running for a
project (any tmux session, any name) are adopted, never duplicated. A server
that is alive but stuck in its reconnect loop -- the device reads "offline" in
claude.ai/code while the process looks healthy -- is restarted once it has been
disconnected for max_disconnected_seconds.

Logouts, network outages and reboots are survived. While Claude Code is logged out
or api.anthropic.com is unreachable, starts are held rather than retried into a
long back-off; exits caused by either never count as crashes. The moment the login
or the network returns, back-off is cleared and every server restarts, which
reconnects its existing sessions ("Environment preserved").

The daemon never deletes anything on disk.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import logging
import os
import re
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("claude-rc-daemon")
DEFAULT_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "claude-rc-daemon" / "config.toml"
CLAUDE_JSON = Path("~/.claude.json").expanduser()
DEFAULT_HOT_PATHS = ["~/Projects"]  # used when the config sets no hot_paths

# ---------------------------------------------------------------- inotify (ctypes)
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_ISDIR = 0x40000000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_EXCL_UNLINK = 0x04000000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
WATCH_MASK = (IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO
              | IN_DELETE_SELF | IN_MOVE_SELF | IN_ONLYDIR | IN_EXCL_UNLINK)
_EVENT_HDR = struct.Struct("iIII")


class Inotify:
    """Minimal non-recursive inotify wrapper. Only directory events reach the caller."""

    def __init__(self) -> None:
        self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.wd_to_path: dict[int, Path] = {}
        self.path_to_wd: dict[Path, int] = {}

    def add(self, path: Path) -> bool:
        if path in self.path_to_wd:
            return True
        wd = self._libc.inotify_add_watch(self.fd, str(path).encode(), WATCH_MASK)
        if wd < 0:
            LOG.warning("cannot watch %s: %s", path, os.strerror(ctypes.get_errno()))
            return False
        self.wd_to_path[wd] = path
        self.path_to_wd[path] = wd
        LOG.info("watching %s", path)
        return True

    def _forget(self, wd: int) -> None:
        path = self.wd_to_path.pop(wd, None)
        if path is not None:
            self.path_to_wd.pop(path, None)
            LOG.warning("hot path went away: %s (will re-watch when it returns)", path)

    def drain(self) -> bool:
        """Read all pending events. Returns True if any relevant (directory) event was seen."""
        relevant = False
        while True:
            try:
                buf = os.read(self.fd, 65536)
            except BlockingIOError:
                return relevant
            off = 0
            while off < len(buf):
                wd, mask, _cookie, length = _EVENT_HDR.unpack_from(buf, off)
                off += _EVENT_HDR.size
                name = buf[off:off + length].split(b"\0", 1)[0].decode(errors="replace")
                off += length
                if mask & IN_IGNORED:
                    self._forget(wd)
                    relevant = True
                elif mask & (IN_DELETE_SELF | IN_MOVE_SELF):
                    relevant = True
                elif mask & IN_ISDIR and not name.startswith("."):
                    relevant = True   # create/delete/move of a sub-folder
                # plain-file events are dropped here: "lonely files" never trigger work


# ---------------------------------------------------------------- config
def _choice(value: object, key: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise SystemExit(f"config: {key} must be one of {', '.join(allowed)}, not {value!r}")
    return str(value)


@dataclass
class Config:
    hot_paths: list[Path]
    exclude: list[str] = field(default_factory=list)
    require_git: bool = True
    claude_args: list[str] = field(default_factory=list)
    session_prefix: str = "rc-"
    settle_seconds: float = 10.0
    reconcile_interval: float = 60.0
    stagger_seconds: float = 3.0
    stop_grace_seconds: float = 10.0
    max_disconnected_seconds: float = 300.0
    max_backoff_seconds: float = 600.0
    network_probe: str = "api.anthropic.com:443"
    recovery_poll_seconds: float = 10.0
    max_log_bytes: int = 5_000_000
    capacity: int = 32
    permission_mode: str = ""
    spawn_mode: str = "worktree"
    remote_all_sessions: bool | None = None
    convert_sessions: bool = False
    restart_on_config_change: bool = True
    log_dir: Path = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "claude-rc-daemon" / "logs"
    settings_template: Path | None = DEFAULT_CONFIG.with_name("settings.local.json")
    auto_trust: bool = False

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        hot = [Path(p).expanduser().resolve() for p in raw.get("hot_paths", []) or DEFAULT_HOT_PATHS]
        return cls(
            hot_paths=hot,
            exclude=list(raw.get("exclude", [])),
            require_git=bool(raw.get("require_git", True)),
            claude_args=list(raw.get("claude_args", [])),
            session_prefix=str(raw.get("session_prefix", "rc-")),
            settle_seconds=float(raw.get("settle_seconds", 10)),
            reconcile_interval=float(raw.get("reconcile_interval", 60)),
            stagger_seconds=float(raw.get("stagger_seconds", 3)),
            stop_grace_seconds=float(raw.get("stop_grace_seconds", 10)),
            max_disconnected_seconds=float(raw.get("max_disconnected_seconds", 300)),
            max_backoff_seconds=float(raw.get("max_backoff_seconds", 600)),
            network_probe=str(raw.get("network_probe", "api.anthropic.com:443")),
            recovery_poll_seconds=float(raw.get("recovery_poll_seconds", 10)),
            max_log_bytes=int(raw.get("max_log_bytes", 5_000_000)),
            capacity=int(raw.get("capacity", 32)),
            permission_mode=str(raw.get("permission_mode", "")),
            spawn_mode=_choice(raw.get("spawn_mode", "worktree"), "spawn_mode", ("worktree", "same-dir", "session")),
            remote_all_sessions=None if raw.get("remote_all_sessions") is None else bool(raw["remote_all_sessions"]),
            convert_sessions=bool(raw.get("convert_sessions", False)),
            restart_on_config_change=bool(raw.get("restart_on_config_change", True)),
            auto_trust=bool(raw.get("auto_trust", False)),
            **({"log_dir": Path(raw["log_dir"]).expanduser()} if "log_dir" in raw else {}),
            **({"settings_template": Path(raw["settings_template"]).expanduser() if raw["settings_template"] else None}
               if "settings_template" in raw else {}),
        )

    def is_excluded(self, p: Path) -> bool:
        for e in self.exclude:
            if "/" in e or e.startswith("~"):
                if Path(e).expanduser().resolve() == p:
                    return True
            elif p.name == e:
                return True
        return False


# ---------------------------------------------------------------- discovery
@dataclass(frozen=True)
class Project:
    path: Path
    git: bool

    @property
    def name(self) -> str:
        return self.path.name

    def spawn(self, mode: str) -> str:
        """The configured spawn mode; worktree needs a git repo, so plain folders fall back to same-dir."""
        return "same-dir" if mode == "worktree" and not self.git else mode


def _is_git(p: Path) -> bool:
    return (p / ".git").exists()


def _is_container(p: Path) -> bool:
    try:
        return any(c.is_dir() and not c.name.startswith(".") and _is_git(c) for c in p.iterdir())
    except OSError:
        return False


def discover(cfg: Config, hinted: set[Path]) -> dict[Path, Project]:
    """Scan hot paths and return the desired project set. `hinted` dedupes container hints."""
    found: dict[Path, Project] = {}
    for hot in cfg.hot_paths:
        if not hot.is_dir():
            continue
        try:
            entries = sorted(hot.iterdir())
        except OSError as exc:
            LOG.warning("cannot list %s: %s", hot, exc)
            continue
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir() or entry.is_symlink():
                continue  # lonely files, dot-dirs and symlinks are ignored
            p = entry.resolve()
            if any(p == h or p in h.parents for h in cfg.hot_paths):
                continue  # a hot path (or its ancestor) is never a project itself
            if cfg.is_excluded(p):
                continue
            if _is_git(p):
                found[p] = Project(p, True)
            elif _is_container(p):
                if p not in hinted:
                    hinted.add(p)
                    LOG.warning("skipping container %s: holds git projects but is not one. "
                                "Add it to hot_paths or exclude it.", p)
            elif not cfg.require_git:
                found[p] = Project(p, False)
    return found


# ---------------------------------------------------------------- running servers
@dataclass
class Server:
    pid: int
    cwd: Path
    cwd_deleted: bool
    argv: tuple[str, ...] = ()


def parent_pids() -> set[int]:
    """Every pid that currently has a child process. A server with children has sessions attached."""
    out: set[int] = set()
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            ppid = _ppid(int(entry.name))
            if ppid:
                out.add(ppid)
    return out


def running_servers() -> dict[Path, Server]:
    """Every `claude remote-control` server process on the box, keyed by its cwd."""
    out: dict[Path, Server] = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
            cwd_raw = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if len(argv) < 2 or os.path.basename(argv[0].decode(errors="replace")) != "claude":
            continue
        if argv[1] != b"remote-control" or b"--help" in argv:
            continue
        deleted = cwd_raw.endswith(" (deleted)")
        cwd = Path(cwd_raw[:-10] if deleted else cwd_raw)
        out.setdefault(cwd, Server(pid, cwd, deleted, tuple(a.decode(errors="replace") for a in argv if a)))
    return out


# ---------------------------------------------------------------- connection state
# A live PID is not a live device. `claude remote-control` retries a lost connection forever
# in-process, so a server can sit for hours with its process healthy while claude.ai/code lists
# the device as offline. The only signal for that is the status line the server paints in its own
# pane: "Connected", or "Reconnecting - retrying in 2.0s - disconnected 9m".
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _pane_sessions() -> dict[int, str]:
    """tmux pane pid -> session name, for every pane on the box."""
    r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}"],
                       capture_output=True, text=True)
    out: dict[int, str] = {}
    if r.returncode != 0:
        return out
    for line in r.stdout.splitlines():
        pid, _, sess = line.partition(" ")
        if pid.isdigit() and sess:
            out[int(pid)] = sess
    return out


def _ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    try:  # comm may contain spaces and parens; state and ppid follow the last ')'
        return int(data[data.rindex(b")") + 2:].split(b" ")[1])
    except (ValueError, IndexError):
        return None


def session_of(pid: int, panes: dict[int, str]) -> str | None:
    """The tmux session owning `pid`, found by walking up its parents to a pane pid."""
    cur: int | None = pid
    for _ in range(12):  # a server sits a couple of levels under its pane; bound the walk anyway
        if cur is None or cur <= 1:
            return None
        if cur in panes:
            return panes[cur]
        cur = _ppid(cur)
    return None


def connection_state(sess: str) -> str | None:
    """'connected' / 'disconnected' from the server's status line, or None when it says neither."""
    r = subprocess.run(["tmux", "capture-pane", "-p", "-t", sess], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    for line in reversed(_ANSI.sub("", r.stdout).splitlines()):
        if "Connected" in line or "Ready" in line:  # newer CLIs paint "✔ Ready · <name>" once linked
            return "connected"
        if "Reconnecting" in line or "disconnected" in line:
            return "disconnected"
    return None


# ---------------------------------------------------------------- preconditions: login and network
# A server started while Claude Code is logged out, or while api.anthropic.com is unreachable, exits
# within seconds. Counting those exits as crashes once pushed every project into an hour-long back-off,
# so servers stayed down long after the login or the network came back. Instead, starts are held while
# a precondition is missing, and everything restarts the moment it returns.
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
CREDENTIALS = CLAUDE_DIR / ".credentials.json"
AUTH_RECHECK_SECONDS = 1800  # re-ask `claude auth status` this often while held on a logout
AUTH_ERRORS = ("You must be logged in", "only available with claude.ai subscriptions")
NETWORK_ERRORS = ("getaddrinfo", "ETIMEOUT", "ENOTFOUND", "EAI_AGAIN", "ECONNREFUSED", "ECONNRESET",
                  "ENETUNREACH", "EHOSTUNREACH", "Server unreachable", "timeout of", "socket hang up")


def credentials_signature() -> tuple[int, int] | None:
    """Changes whenever Claude Code logs in, logs out or refreshes its token."""
    try:
        st = CREDENTIALS.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def logged_in() -> bool | None:
    """`claude auth status` -> loggedIn. None when it cannot be asked or answers in an unknown form."""
    try:
        r = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        return bool(json.loads(r.stdout).get("loggedIn"))
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return None


def probe_target(spec: str) -> tuple[str, int] | None:
    """host:port whose reachability means "online": the HTTPS proxy when one is set. None disables."""
    if not spec:
        return None
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        u = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        if u.hostname:
            return u.hostname, u.port or (443 if u.scheme == "https" else 80)
    host, _, port = spec.rpartition(":")
    return (host, int(port)) if host and port.isdigit() else (spec, 443)


def network_up(target: tuple[str, int], timeout: float = 5.0) -> bool:
    """One TCP connect, in a thread so a hanging DNS lookup cannot stall the daemon."""
    ok: list[bool] = []

    def attempt() -> None:
        try:
            with socket.create_connection(target, timeout=timeout):
                ok.append(True)
        except OSError:
            pass

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(timeout * 3)
    return bool(ok)


def classify_exit(logfile: Path | None, offset: int) -> str:
    """'auth', 'network' or 'crash', from what a server printed to its log since it was started."""
    if logfile is None:
        return "crash"
    try:
        with open(logfile, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(offset if offset <= size else 0, size - 65536))
            text = _ANSI.sub("", fh.read().decode(errors="replace"))
    except OSError:
        return "crash"
    if any(s in text for s in AUTH_ERRORS):
        return "auth"
    if any(s in text for s in NETWORK_ERRORS):
        return "network"
    return "crash"


# ---------------------------------------------------------------- remote control for every CLI session
USER_SETTINGS = CLAUDE_DIR / "settings.json"


def ensure_remote_at_startup(want: bool | None, dry_run: bool) -> None:
    """Keep `remoteControlAtStartup` in the user settings at `want`, so every interactive `claude`
    session starts with Remote Control on (or off). Re-applied on every reconcile, so a reset does not
    stick. None leaves the setting to the user."""
    if want is None:
        return
    try:
        data = json.loads(USER_SETTINGS.read_text()) if USER_SETTINGS.exists() else {}
    except (OSError, ValueError) as exc:
        LOG.warning("cannot read %s, not touching it: %s", USER_SETTINGS, exc)
        return
    if not isinstance(data, dict) or data.get("remoteControlAtStartup") is want:
        return
    if dry_run:
        LOG.info("[dry-run] would set remoteControlAtStartup = %s in %s", want, USER_SETTINGS)
        return
    data["remoteControlAtStartup"] = want
    try:
        mode = USER_SETTINGS.stat().st_mode & 0o777 if USER_SETTINGS.exists() else 0o600
        USER_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        tmp = USER_SETTINGS.with_name(".settings.json.rc-daemon.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, USER_SETTINGS)
    except OSError as exc:
        LOG.warning("cannot write %s: %s", USER_SETTINGS, exc)
        return
    LOG.info("set remoteControlAtStartup = %s in %s", want, USER_SETTINGS)


# Sessions opened before Remote Control was switched on stay local. Claude Code offers no external
# switch, and TIOCSTI injection is off on current kernels, so the daemon types `/remote-control` into
# the session's tmux pane -- only while the session is idle and its prompt holds no typed text.
SESSIONS_DIR = CLAUDE_DIR / "sessions"
_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_RULE = re.compile(r"^\s*─{8,}\s*$")


@dataclass
class CliSession:
    pid: int
    cwd: str
    status: str
    remote: bool


def cli_sessions() -> list[CliSession]:
    """Live interactive `claude` sessions, from the registry Claude Code keeps in ~/.claude/sessions."""
    out: list[CliSession] = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        pid = d.get("pid") if isinstance(d, dict) else None
        if not isinstance(pid, int) or d.get("kind") != "interactive" or d.get("entrypoint") != "cli":
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue  # stale record of an exited session
        if os.path.basename(argv[0].decode(errors="replace")) != "claude" or b"remote-control" in argv:
            continue
        out.append(CliSession(pid, str(d.get("cwd", "")), str(d.get("status", "")), bool(d.get("bridgeSessionId"))))
    return out


def _panes() -> dict[int, tuple[str, str]]:
    """tmux pane pid -> (pane id, session name), for every pane on the box."""
    r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{pane_id} #{session_name}"],
                       capture_output=True, text=True)
    out: dict[int, tuple[str, str]] = {}
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        pid, _, rest = line.partition(" ")
        pane, _, sess = rest.partition(" ")
        if pid.isdigit() and pane:
            out[int(pid)] = (pane, sess)
    return out


def _typed_text(s: str) -> str:
    """Visible text that is not dim. Claude Code paints its placeholder suggestion dim."""
    s = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", s)
    out, dim, pos = [], False, 0
    for m in _SGR.finditer(s):
        if not dim:
            out.append(s[pos:m.start()])
        for code in (m.group(1) or "0").split(";"):
            if code in ("0", "", "22"):
                dim = False
            elif code == "2":
                dim = True
        pos = m.end()
    if not dim:
        out.append(s[pos:])
    return _ANSI.sub("", "".join(out)).strip()


def prompt_is_empty(pane: str) -> bool:
    """True when the pane shows Claude Code's input box (a ❯ line between two rules) with nothing typed."""
    r = subprocess.run(["tmux", "capture-pane", "-p", "-e", "-t", pane], capture_output=True, text=True)
    if r.returncode != 0:
        return False
    raw = r.stdout.rstrip("\n").splitlines()
    plain = [_ANSI.sub("", line) for line in raw]
    for i in range(len(raw) - 2, 0, -1):
        if plain[i].lstrip().startswith("❯"):
            if not (_RULE.match(plain[i - 1]) and _RULE.match(plain[i + 1])):
                return False  # a ❯ outside the input box: a menu or dialog is open
            return _typed_text(raw[i][raw[i].index("❯") + 1:]) == ""
    return False


# ---------------------------------------------------------------- workspace trust
def _read_claude_json() -> dict:
    try:
        return json.loads(CLAUDE_JSON.read_text()) if CLAUDE_JSON.exists() else {}
    except (OSError, ValueError) as exc:
        LOG.warning("cannot read %s: %s", CLAUDE_JSON, exc)
        return {}


def trusted_paths() -> set[Path]:
    """Folders whose workspace-trust dialog has been accepted. `claude remote-control` refuses others."""
    projects = _read_claude_json().get("projects", {})
    return {Path(p) for p, v in projects.items() if isinstance(v, dict) and v.get("hasTrustDialogAccepted") is True}


def grant_trust(paths: list[Path]) -> None:
    """Record trust for the given folders, exactly as accepting the dialog in each would.
    Invoked by the explicit `--trust` command, or by the loop when `auto_trust = true`."""
    data = _read_claude_json()
    projects = data.setdefault("projects", {})
    for p in paths:
        projects.setdefault(str(p), {})["hasTrustDialogAccepted"] = True
    backup = CLAUDE_JSON.with_name(".claude.json.rc-daemon.bak")
    if CLAUDE_JSON.exists():
        backup.write_bytes(CLAUDE_JSON.read_bytes())
    tmp = CLAUDE_JSON.with_name(".claude.json.rc-daemon.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CLAUDE_JSON)


# ---------------------------------------------------------------- actions
def session_name(cfg: Config, project: Project, taken: set[str]) -> str:
    base = cfg.session_prefix + project.name
    if base not in taken:
        return base
    return f"{base}-{hashlib.sha1(str(project.path).encode()).hexdigest()[:6]}"


def tmux_sessions() -> set[str]:
    r = subprocess.run(["tmux", "ls", "-F", "#S"], capture_output=True, text=True)
    return set(r.stdout.split()) if r.returncode == 0 else set()


def server_command(cfg: Config, project: Project) -> list[str]:
    spawn = project.spawn(cfg.spawn_mode)
    cmd = ["claude", "remote-control", "--name", project.name, "--spawn", spawn]
    if cfg.capacity > 0 and spawn != "session" and "--capacity" not in cfg.claude_args:  # session mode: 1 only
        cmd += ["--capacity", str(cfg.capacity)]
    if cfg.permission_mode and "--permission-mode" not in cfg.claude_args:
        cmd += ["--permission-mode", cfg.permission_mode]
    return cmd + cfg.claude_args


SETTINGS_LOCAL = Path(".claude") / "settings.local.json"


def seed_settings(cfg: Config, project: Project, dry_run: bool) -> None:
    """Copy the settings template to <project>/.claude/settings.local.json and git-exclude it.
    A project's existing file holds its own choices and is never overwritten."""
    tpl, dest = cfg.settings_template, project.path / SETTINGS_LOCAL
    if tpl is None or not tpl.is_file() or dest.exists():
        return
    if dry_run:
        LOG.info("[dry-run] would seed %s from %s", dest, tpl)
        return
    try:
        dest.parent.mkdir(exist_ok=True)
        tmp = dest.with_name(dest.name + ".rc-daemon.tmp")
        tmp.write_bytes(tpl.read_bytes())
        os.replace(tmp, dest)
    except OSError as exc:
        LOG.warning("cannot seed %s: %s", dest, exc)
        return
    LOG.info("seeded %s from %s", dest, tpl)
    if not project.git:
        return
    r = subprocess.run(["git", "-C", str(project.path), "rev-parse", "--git-path", "info/exclude"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return
    exclude, entry = project.path / r.stdout.strip(), f"/{SETTINGS_LOCAL}"
    try:
        text = exclude.read_text() if exclude.exists() else ""
        if entry not in text.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with open(exclude, "a") as fh:
                fh.write(("" if not text or text.endswith("\n") else "\n") + entry + "\n")
    except OSError as exc:
        LOG.warning("cannot add %s to %s: %s", entry, exclude, exc)


def start(cfg: Config, project: Project, taken: set[str], dry_run: bool) -> tuple[Path | None, int]:
    """Start a server. Returns its log file and the offset its output begins at."""
    sess = session_name(cfg, project, taken)
    cmd = server_command(cfg, project)
    if dry_run:
        LOG.info("[dry-run] would start %s in %s: %s", sess, project.path, shlex.join(cmd))
        return None, 0
    logfile: Path | None = cfg.log_dir / f"{sess}.log"
    offset = 0
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        if cfg.max_log_bytes > 0 and logfile.exists() and logfile.stat().st_size > cfg.max_log_bytes:
            os.replace(logfile, logfile.with_name(logfile.name + ".1"))  # keep one older generation
        offset = logfile.stat().st_size if logfile.exists() else 0
    except OSError as exc:
        LOG.warning("cannot prepare log for %s: %s", sess, exc)
        logfile = None
    subprocess.run(["tmux", "new", "-d", "-s", sess, "-c", str(project.path), shlex.join(cmd)], check=True)
    taken.add(sess)
    # Mirror the pane to a log file so an early exit (auth, network, trust prompt) stays diagnosable,
    # and so the daemon can tell why the server exited.
    if logfile is not None:
        subprocess.run(["tmux", "pipe-pane", "-t", sess, "-o", f"cat >> {shlex.quote(str(logfile))}"], check=False)
    LOG.info("started %s for %s (%s)", sess, project.path, project.spawn(cfg.spawn_mode))
    return logfile, offset


def stop(server: Server, cfg: Config, dry_run: bool, why: str | None = None) -> None:
    if why is None:
        why = "folder deleted" if server.cwd_deleted else "no longer a tracked project"
    if dry_run:
        LOG.info("[dry-run] would stop pid %d in %s (%s)", server.pid, server.cwd, why)
        return
    LOG.info("stopping pid %d in %s (%s)", server.pid, server.cwd, why)
    try:
        os.kill(server.pid, signal.SIGINT)
        deadline = time.monotonic() + cfg.stop_grace_seconds
        while time.monotonic() < deadline:
            os.kill(server.pid, 0)
            time.sleep(0.2)
        os.kill(server.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------- reconcile loop
class Daemon:
    def __init__(self, cfg: Config, dry_run: bool) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.hinted: set[Path] = set()
        self.failures: dict[Path, int] = {}
        self.next_allowed: dict[Path, float] = {}
        self.started_at: dict[Path, float] = {}
        self.disconnected_since: dict[Path, float] = {}
        self.stopping = False
        self.warned_untrusted: set[Path] = set()
        # per started server: log file, offset its output starts at, credentials signature at start
        self.logs: dict[Path, tuple[Path | None, int, tuple[int, int] | None]] = {}
        self.creds_sig: object = object()  # never equal to a real signature, so the first check asks
        self.login_ok: bool | None = None  # None: unknown, which does not hold starts
        self.login_recheck_at = 0.0
        self.probe = probe_target(cfg.network_probe)
        self.online = True
        self.held: str | None = None  # reason starts are held, as last logged
        self.drift_deferred: set[Path] = set()
        self.convert_after: dict[int, float] = {}  # claude session pid -> earliest next conversion try
        self.not_in_tmux: set[int] = set()  # sessions already reported as unreachable

    # ------------------------------------------------ preconditions
    def hold_reason(self) -> str | None:
        if self.login_ok is False:
            return "Claude Code is logged out -- run `claude auth login` (or /login in claude)"
        if not self.online and self.probe is not None:
            return f"network down ({self.probe[0]}:{self.probe[1]} unreachable)"
        return None

    def note_hold(self) -> str | None:
        reason = self.hold_reason()
        if reason != self.held and reason is not None:
            LOG.warning("holding all server starts: %s", reason)
        self.held = reason
        return reason

    def preflight(self) -> bool:
        """Refresh login and network state. True when a missing precondition just came back, in
        which case back-off is cleared so every server restarts now instead of hours from now."""
        was = self.hold_reason()
        now = time.monotonic()
        sig = credentials_signature()
        if sig != self.creds_sig or (self.login_ok is False and now >= self.login_recheck_at):
            self.creds_sig = sig
            self.login_ok = logged_in()
            self.login_recheck_at = now + AUTH_RECHECK_SECONDS
        if self.probe is not None:
            self.online = network_up(self.probe)
        self.note_hold()
        if was is not None and self.held is None:
            LOG.info("recovered (was: %s); clearing back-off and restarting servers", was)
            self.failures.clear()
            self.next_allowed.clear()
            return True
        return False

    def reconcile(self) -> None:
        ensure_remote_at_startup(self.cfg.remote_all_sessions, self.dry_run)
        self.preflight()
        desired = discover(self.cfg, self.hinted)
        running = running_servers()
        now = time.monotonic()
        taken = tmux_sessions()
        trusted = trusted_paths()

        # Untrusted folders would just print "Workspace not trusted" and exit; do not start them.
        untrusted = [p for p in desired if p not in trusted and p not in running]
        if untrusted and self.cfg.auto_trust:
            # The user opted in by listing the hot path in the config; treat that as accepting the
            # workspace-trust dialog for every project folder found directly inside it.
            if self.dry_run:
                LOG.info("[dry-run] would record trust for: %s", ", ".join(p.name for p in untrusted))
            else:
                grant_trust(untrusted)
                LOG.info("auto_trust: recorded trust for %s", ", ".join(p.name for p in untrusted))
                trusted = trusted_paths()
                untrusted = [p for p in desired if p not in trusted and p not in running]
        new_untrusted = [p for p in untrusted if p not in self.warned_untrusted]
        if new_untrusted:
            self.warned_untrusted.update(new_untrusted)
            LOG.warning("%d project(s) not yet trusted, skipping: %s. Run `claude-rc-daemon --trust` to "
                        "accept them all at once, set auto_trust = true, or run `claude` once in each folder.",
                        len(new_untrusted), ", ".join(p.name for p in new_untrusted))
        desired = {p: pr for p, pr in desired.items() if p not in untrusted}
        for project in desired.values():
            seed_settings(self.cfg, project, self.dry_run)

        # Stop servers whose folder vanished, or that sit directly under a hot path without being a
        # tracked project (a container like SideProjects, or an excluded folder). Servers you started
        # by hand deeper in the tree, or outside the hot paths, are left alone.
        stopped: set[Path] = set()
        for cwd, srv in running.items():
            if srv.cwd_deleted or (cwd.parent in self.cfg.hot_paths and cwd not in desired
                                   and cwd not in self.cfg.hot_paths):
                stop(srv, self.cfg, self.dry_run)
                stopped.add(cwd)

        # A running process is not an online device: restart the ones stuck reconnecting. Not while
        # held -- a fresh server could not connect either; the old one's own retry loop is better.
        if self.held is None:
            self.check_connections({p: s for p, s in running.items() if p in desired and p not in stopped}, now)
            if self.cfg.restart_on_config_change:
                self.apply_config_changes({p: s for p, s in running.items() if p in desired and p not in stopped},
                                          desired)

        # Notice servers that died since the last pass. An exit caused by a logout or an outage holds
        # all starts; only a genuine crash counts against the project's back-off.
        offline: bool | None = None
        for path in [p for p in self.started_at if p in desired]:
            if path in running and not running[path].cwd_deleted:
                if now - self.started_at[path] > 120:
                    self.failures.pop(path, None)
                    self.next_allowed.pop(path, None)
                    self.started_at.pop(path, None)
                    self.logs.pop(path, None)
                continue
            self.started_at.pop(path)
            logfile, offset, sig = self.logs.pop(path, (None, 0, None))
            kind = classify_exit(logfile, offset)
            if kind == "auth":
                LOG.info("server for %s exited: not logged in", path)
                if sig == credentials_signature():  # a login since this start would make it retryable
                    self.login_ok = False
                    self.login_recheck_at = now + AUTH_RECHECK_SECONDS
                continue
            if kind == "network" and self.probe is not None:
                if offline is None:
                    offline = not network_up(self.probe)
                if offline:
                    LOG.info("server for %s exited: network unreachable", path)
                    self.online = False
                    continue
            n = self.failures.get(path, 0) + 1
            self.failures[path] = n
            delay = min(30 * 2 ** n, self.cfg.max_backoff_seconds)
            self.next_allowed[path] = now + delay
            LOG.warning("server for %s exited (%s, %d failures); retry in %ds. See %s, or run `claude` once "
                        "in that folder if it stalled on the workspace-trust prompt.",
                        path, kind, n, delay, logfile or self.cfg.log_dir)
        if self.note_hold() is not None:
            return  # nothing started now would stay up; preflight polls for the recovery
        if self.cfg.convert_sessions:
            self.convert_open_sessions()

        # Start what is missing, with back-off for servers that keep dying.
        started_any = False
        for path, project in desired.items():
            if path in running and not running[path].cwd_deleted:
                continue
            if now < self.next_allowed.get(path, 0):
                continue
            if self.stopping:
                return  # a stop request arrived mid-rollout; leave the rest for the next daemon start
            if started_any and not self.dry_run:
                time.sleep(self.cfg.stagger_seconds)
            try:
                logfile, offset = start(self.cfg, project, taken, self.dry_run)
                if not self.dry_run:
                    self.started_at[path] = time.monotonic()
                    self.logs[path] = (logfile, offset, credentials_signature())
                started_any = True
            except subprocess.CalledProcessError as exc:
                LOG.error("tmux failed for %s: %s", path, exc)

    def convert_open_sessions(self) -> None:
        """Turn on Remote Control in interactive `claude` sessions that are running without it."""
        local = [s for s in cli_sessions() if not s.remote]
        live = {s.pid for s in local}
        self.convert_after = {p: t for p, t in self.convert_after.items() if p in live}
        self.not_in_tmux &= live
        if not local:
            return
        now = time.monotonic()
        # Server panes need no filter: cli_sessions() skips servers, and their sessions are remote.
        pane_ids = {pid: pane for pid, (pane, _sess) in _panes().items()}
        for s in local:
            if now < self.convert_after.get(s.pid, 0):
                continue
            pane = session_of(s.pid, pane_ids)
            if pane is None:
                if s.pid not in self.not_in_tmux:
                    self.not_in_tmux.add(s.pid)
                    LOG.warning("claude session pid %d in %s has no Remote Control and is not in tmux, so it "
                                "cannot be switched from outside; run /remote-control in it", s.pid, s.cwd)
                continue
            if s.status != "idle" or not prompt_is_empty(pane):
                continue  # busy, waiting on a dialog, or you are typing: try again next pass
            if self.dry_run:
                LOG.info("[dry-run] would enable Remote Control in claude session pid %d (%s, tmux %s)",
                         s.pid, s.cwd, pane)
                continue
            subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "/remote-control"], check=False)
            time.sleep(0.5)  # let the slash-command menu settle before Enter picks it
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=False)
            self.convert_after[s.pid] = now + 600  # if it does not take, retry later rather than every pass
            LOG.info("enabled Remote Control in claude session pid %d (%s, tmux %s)", s.pid, s.cwd, pane)

    def apply_config_changes(self, running: dict[Path, Server], desired: dict[Path, Project]) -> None:
        """Restart servers whose command line no longer matches the config (capacity, permission_mode,
        claude_args), so edits take effect. A server with sessions attached is left until it is idle;
        the next reconcile starts the replacement."""
        drifted = {p: s for p, s in running.items()
                   if s.argv and list(s.argv[1:]) != server_command(self.cfg, desired[p])[1:]}
        if not drifted:
            return
        busy = parent_pids()
        for path, srv in drifted.items():
            if srv.pid in busy:
                if path not in self.drift_deferred:
                    self.drift_deferred.add(path)
                    LOG.info("config changed for %s; restart deferred until its sessions end", path)
                continue
            self.drift_deferred.discard(path)
            stop(srv, self.cfg, self.dry_run, why="config changed: " + shlex.join(server_command(self.cfg, desired[path])))

    def links(self, running: dict[Path, Server]) -> dict[Path, str | None]:
        """Connection state per running server. One tmux call plus one capture per server."""
        panes = _pane_sessions()
        out: dict[Path, str | None] = {}
        for cwd, srv in running.items():
            sess = session_of(srv.pid, panes)
            out[cwd] = connection_state(sess) if sess else None
        return out

    def check_connections(self, running: dict[Path, Server], now: float) -> None:
        """Restart servers whose process is alive but whose link to Claude is not.

        `claude remote-control` retries a dropped connection in-process and never exits, so the
        PID check above keeps calling it healthy while claude.ai/code lists the device as offline.
        Once a server has read "disconnected" for max_disconnected_seconds we stop it; the next
        reconcile starts a fresh one. A pane that says neither (trust prompt, error, startup) is
        left alone -- only an explicit reconnect loop counts.
        """
        if self.cfg.max_disconnected_seconds <= 0:
            return
        for cwd, state in self.links(running).items():
            if state != "disconnected":
                since = self.disconnected_since.pop(cwd, None)
                if since is not None and state == "connected":
                    LOG.info("server for %s reconnected after %ds", cwd, int(now - since))
                continue
            since = self.disconnected_since.setdefault(cwd, now)
            stalled = now - since
            if stalled < self.cfg.max_disconnected_seconds:
                continue
            LOG.warning("server for %s is alive but has been disconnected for %ds -- the device reads "
                        "offline in claude.ai/code; restarting it", cwd, int(stalled))
            stop(running[cwd], self.cfg, self.dry_run, why="disconnected from Claude")
            if not self.dry_run:
                self.disconnected_since.pop(cwd, None)

    def status(self) -> None:
        desired = discover(self.cfg, self.hinted)
        running = running_servers()
        trusted = trusted_paths()
        links = self.links(running)
        login = logged_in()
        net = "unchecked" if self.probe is None else ("up" if network_up(self.probe) else "DOWN")
        print(f"login: {'yes' if login else 'NO' if login is False else 'unknown'}    network: {net}\n")
        print(f"{'STATE':<10} {'LINK':<12} {'PID':>7}  PATH")
        for path in sorted(set(desired) | {p for p in running if p.parent in self.cfg.hot_paths}):
            srv = running.get(path)
            link = links.get(path) or ("" if srv is None else "unknown")
            if path in desired and srv:
                state = "stalled" if link == "disconnected" else "running"
            elif path in desired and path not in trusted:
                state = "untrusted"
            elif path in desired:
                state = "missing"
            else:
                state = "stray"
            print(f"{state:<10} {link:<12} {srv.pid if srv else '':>7}  {path}")

    def trust(self, yes: bool) -> None:
        desired = discover(self.cfg, self.hinted)
        trusted = trusted_paths()
        todo = sorted(p for p in desired if p not in trusted)
        if not todo:
            print("all tracked projects are already trusted")
            return
        print("Not yet trusted:")
        for p in todo:
            print(f"  {p}")
        if not yes:
            if not sys.stdin.isatty():
                raise SystemExit("re-run with --yes to record trust for the folders above")
            if input(f"Record workspace trust for these {len(todo)} folders in {CLAUDE_JSON}? [y/N] ").strip().lower() != "y":
                raise SystemExit("aborted")
        grant_trust(todo)
        print(f"trusted {len(todo)} folder(s); backup at {CLAUDE_JSON.with_name('.claude.json.rc-daemon.bak')}")
        print("the daemon picks them up on its next reconcile (within reconcile_interval)")

    def run(self) -> None:
        ino = Inotify()

        def _sig(*_a):
            self.stopping = True

        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)
        # Python retries select() after a signal, so without this a stop waited out the whole
        # reconcile interval. The wakeup fd makes a signal readable, ending select() at once.
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_r, False)
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)

        next_reconcile = time.monotonic()
        while not self.stopping:
            for hot in self.cfg.hot_paths:
                if hot.is_dir():
                    ino.add(hot)
            now = time.monotonic()
            if now >= next_reconcile:
                try:
                    self.reconcile()
                except Exception:  # keep the loop alive; a bad scan must not kill the daemon
                    LOG.exception("reconcile failed")
                next_reconcile = time.monotonic() + self.cfg.reconcile_interval
            timeout = max(0.0, next_reconcile - time.monotonic())
            if self.held is not None:
                timeout = min(timeout, self.cfg.recovery_poll_seconds)
            try:
                ready, _, _ = select.select([ino.fd, wake_r], [], [], timeout)
            except InterruptedError:
                continue
            if wake_r in ready:
                try:
                    os.read(wake_r, 64)
                except BlockingIOError:
                    pass
            if ino.fd in ready and ino.drain():
                # A sub-folder appeared/vanished: let git clone etc. settle, then reconcile early.
                next_reconcile = min(next_reconcile, time.monotonic() + self.cfg.settle_seconds)
            if self.held is not None and not self.stopping:
                # Logged out or offline: poll so servers come back seconds after login/network does.
                try:
                    if self.preflight():
                        next_reconcile = time.monotonic()
                except Exception:
                    LOG.exception("preflight failed")
        LOG.info("daemon exiting; remote-control servers keep running in tmux")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--once", action="store_true", help="reconcile once and exit")
    ap.add_argument("--dry-run", action="store_true", help="log actions without starting/stopping anything")
    ap.add_argument("--status", action="store_true", help="print projects and their server state")
    ap.add_argument("--trust", action="store_true",
                    help="record workspace trust for every tracked project that lacks it (asks first)")
    ap.add_argument("--yes", action="store_true", help="with --trust: do not ask")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if not args.config.exists():
        raise SystemExit(f"config not found: {args.config}")
    cfg = Config.load(args.config)
    d = Daemon(cfg, args.dry_run)
    if args.status:
        d.status()
    elif args.trust:
        d.trust(args.yes)
    elif args.once:
        d.reconcile()
    else:
        d.run()


if __name__ == "__main__":
    main()
