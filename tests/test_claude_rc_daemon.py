"""Tests for claude_rc_daemon. Run from the repo root: python3 -m unittest discover -s tests

Screens under fixtures/ were recorded from Claude Code 2.1.271 (servers) and 2.1.272 (sessions). The daemon reads text that Claude Code
paints for people, so when a release changes that wording, refresh the fixtures and these tests show
what broke (issue #4).
"""
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_rc_daemon as d  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def screen(text: str):
    """Make every subprocess.run (tmux capture-pane, send-keys) answer with `text`."""
    return mock.patch.object(d.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 0, stdout=text, stderr=""))


class ServerScreen(unittest.TestCase):
    """The status line stall detection depends on (issue #4)."""

    def state(self, fixture: str) -> str | None:
        with screen((FIXTURES / fixture).read_text()):
            return d.connection_state("rc-x")

    def test_ready(self):
        self.assertEqual(self.state("server_ready.txt"), "connected")

    def test_connected(self):
        self.assertEqual(self.state("server_connected.txt"), "connected")

    def test_reconnecting(self):
        self.assertEqual(self.state("server_reconnecting.txt"), "disconnected")

    def test_confirmation_prompt(self):
        self.assertEqual(self.state("server_prompt.txt"), "prompt")

    def test_unrecognised_wording(self):
        with screen("Something Claude Code says in a later release\n"):
            self.assertIsNone(d.connection_state("rc-x"))

    def test_warns_once_after_three_passes_with_no_recognised_line(self):
        dm = d.Daemon(d.Config(hot_paths=[]), dry_run=True)
        with self.assertLogs(d.LOG, "WARNING") as logs:
            for _ in range(5):
                dm.check_wording({Path("/p/a"): None, Path("/p/b"): None})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("wording", logs.output[0])

    def test_recognised_line_resets_the_count(self):
        dm = d.Daemon(d.Config(hot_paths=[]), dry_run=True)
        for state in (None, None, "connected", None, None):
            dm.check_wording({Path("/p/a"): state})
        self.assertFalse(dm.wording_warned)

    def test_prompt_answered_once_per_server(self):
        dm = d.Daemon(d.Config(hot_paths=[]), dry_run=False)
        dm.pane_of[Path("/p/a")] = "rc-a"
        srv = d.Server(4242, Path("/p/a"), False)
        with screen("") as run, self.assertLogs(d.LOG, "INFO"):
            dm.answer_prompt(Path("/p/a"), srv)
            dm.answer_prompt(Path("/p/a"), srv)
        run.assert_called_once_with(["tmux", "send-keys", "-t", "rc-a", "y", "Enter"], check=False)


class ExitClassification(unittest.TestCase):
    def classify(self, text: str, offset: int = 0) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "rc.log"
            log.write_text(text)
            return d.classify_exit(log, offset)

    def test_recorded_logout(self):
        self.assertEqual(self.classify((FIXTURES / "server_logged_out.ansi").read_text(errors="replace"))[0], "auth")

    def test_outage(self):
        kind, why = self.classify("\x1b[31mError: Server unreachable for 11 minutes, giving up.\x1b[0m\n")
        self.assertEqual((kind, why), ("network", "Error: Server unreachable for 11 minutes, giving up."))

    def test_environment_refusal(self):
        kind, why = self.classify("Error: Remote Control requires feature-flag evaluation, which is disabled "
                                  "because DISABLE_GROWTHBOOK is set.\n")
        self.assertEqual(kind, "env")
        self.assertIn("DISABLE_GROWTHBOOK", why)

    def test_crash_reports_last_error_line(self):
        self.assertEqual(self.classify("starting\nError: boom\nbye\n"), ("crash", "Error: boom"))

    def test_reads_only_output_since_start(self):
        old = "Error: You must be logged in to use Remote Control.\n"
        self.assertEqual(self.classify(old + "Error: boom\n", len(old)), ("crash", "Error: boom"))

    def test_no_log(self):
        self.assertEqual(d.classify_exit(None, 0), ("crash", ""))


class Environment(unittest.TestCase):
    def test_blocking_variables(self):
        found = d.environment_problems({"DISABLE_GROWTHBOOK": "1", "ANTHROPIC_BASE_URL": "https://proxy.corp",
                                        "HOME": "/home/u"})
        self.assertEqual(found, ["DISABLE_GROWTHBOOK", "ANTHROPIC_BASE_URL"])

    def test_default_base_url_is_fine(self):
        self.assertEqual(d.environment_problems({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}), [])


class CliSessionScreen(unittest.TestCase):
    """What must be true before the daemon types /remote-control into someone's session."""

    def empty(self, fixture: str) -> bool:
        with screen((FIXTURES / fixture).read_text()):
            return d.prompt_is_empty("%1")

    def test_placeholder_counts_as_empty(self):
        self.assertTrue(self.empty("cli_idle_empty.ansi"))

    def test_draft_blocks_typing(self):
        self.assertFalse(self.empty("cli_idle_draft.ansi"))

    def test_dialog_blocks_typing(self):
        with screen(" Do you want to proceed?\n❯ 1. Yes\n  2. No\n"):
            self.assertFalse(d.prompt_is_empty("%1"))

    def test_dim_text_is_not_typed(self):
        self.assertEqual(d._typed_text('\x1b[2mTry "fix lint errors"\x1b[22m'), "")
        self.assertEqual(d._typed_text(" hello\x1b[7m \x1b[0m"), "hello")

    def test_link_states(self):
        cases = {
            "~/p · /rc active\n": "active",
            "~/p · /rc reconnecting\n": "connecting",
            "~/p · /rc failed\n": "failed",
            "/remote-control is active · Continue here\n/remote-control is no longer active. Run /remote-control "
            "to start a new session.\n": "failed",
            "Remote Control disconnected.\n/remote-control is active · Continue here\n": None,
            "❯ \n": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text), screen(text):
                self.assertEqual(d.cli_link_state("%1"), expected)


class ServerCommand(unittest.TestCase):
    def cmd(self, mode: str, git: bool = True, **kw) -> list[str]:
        return d.server_command(d.Config(hot_paths=[], spawn_mode=mode, **kw), d.Project(Path("/p/app"), git))

    def test_worktree(self):
        self.assertEqual(self.cmd("worktree", permission_mode="auto"),
                         ["claude", "remote-control", "--name", "app", "--spawn", "worktree",
                          "--capacity", "32", "--permission-mode", "auto"])

    def test_plain_folder_falls_back_to_same_dir(self):
        self.assertEqual(self.cmd("worktree", git=False)[5], "same-dir")

    def test_session_mode_has_no_capacity(self):
        self.assertNotIn("--capacity", self.cmd("session"))

    def test_machine_remote_uses_its_label(self):
        cmd = d.server_command(d.Config(hot_paths=[]), d.Project(Path("/home/u/Projects"), False, "debian"))
        self.assertEqual(cmd[3], "debian")


class SeedSettings(unittest.TestCase):
    """Issue #1."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # keep the user's global gitignore out of it
        self.env = mock.patch.dict(os.environ, {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "xdg"),
                                                "GIT_CONFIG_NOSYSTEM": "1"})
        self.env.start()
        self.repo = root / "app"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.tpl = root / "template.json"
        self.tpl.write_text('{"permissions": {"defaultMode": "auto"}}\n')
        self.cfg = d.Config(hot_paths=[root], settings_template=self.tpl)
        self.project = d.Project(self.repo, True)
        self.dest = self.repo / ".claude" / "settings.local.json"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def seed(self, dry_run: bool = False) -> str:
        with self.assertLogs(d.LOG, "INFO"):
            return d.seed_settings(self.cfg, self.project, dry_run)

    def test_creates_and_excludes_once(self):
        self.assertEqual(self.seed(), "created")
        self.assertEqual(self.dest.read_text(), self.tpl.read_text())
        self.dest.unlink()
        self.assertEqual(self.seed(), "created")
        exclude = (self.repo / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertEqual(exclude.count("/.claude/settings.local.json"), 1)

    def test_never_overwrites(self):
        self.dest.parent.mkdir()
        self.dest.write_text("{}")
        self.assertEqual(d.seed_settings(self.cfg, self.project, False), "exists")
        self.assertEqual(self.dest.read_text(), "{}")

    def test_already_ignored_adds_no_exclude(self):
        (self.repo / ".gitignore").write_text(".claude/settings.local.json\n")
        self.assertEqual(self.seed(), "created")
        exclude = self.repo / ".git" / "info" / "exclude"
        self.assertNotIn("settings.local.json", exclude.read_text() if exclude.exists() else "")

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.seed(dry_run=True), "would-create")
        self.assertFalse(self.dest.parent.exists())

    def test_no_template(self):
        self.cfg.settings_template = None
        self.assertEqual(d.seed_settings(self.cfg, self.project, False), "no-template")


class HoldAndRecovery(unittest.TestCase):
    """Logouts and outages hold starts instead of feeding back-off (issue #3); only genuine crashes back
    off, per folder, and a folder that keeps crashing is marked failing (issues #3, #5)."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.TemporaryDirectory()
        self.world = {"login": True, "online": True, "sig": (1, 1), "running": set(), "exit": ("crash", "Error: boom")}
        self.started: list[str] = []
        w = self.world
        projects = {Path(f"/p/{n}"): d.Project(Path(f"/p/{n}"), True) for n in ("a", "b")}

        def start(cfg, project, taken, dry_run):
            self.started.append(project.name)
            return None, 0

        self.patcher = mock.patch.multiple(
            d, discover=lambda cfg, hinted: dict(projects), trusted_paths=lambda: set(projects),
            tmux_sessions=lambda: set(), logged_in=lambda: w["login"],
            network_up=lambda target, timeout=5.0: w["online"], credentials_signature=lambda: w["sig"],
            running_servers=lambda: {p: d.Server(1, p, False) for p in w["running"]},
            start=start, classify_exit=lambda logfile, offset: w["exit"],
            seed_settings=lambda *a: "exists", ensure_remote_at_startup=lambda *a: None)
        self.patcher.start()
        cfg = d.Config(hot_paths=[Path("/p")], stagger_seconds=0, log_dir=Path(self.tmp.name) / "logs")
        self.dm = d.Daemon(cfg, dry_run=False)
        self.dm.check_connections = lambda running, now: None
        self.dm.apply_config_changes = lambda running, desired: None

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def step(self) -> list[str]:
        self.started.clear()
        self.dm.reconcile()
        return sorted(self.started)

    def test_logout_holds_then_login_restarts_everything(self):
        self.assertEqual(self.step(), ["a", "b"])
        self.world["exit"] = ("auth", "Error: You must be logged in to use Remote Control.")
        self.assertEqual(self.step(), [])
        self.assertIn("logged out", self.dm.held)
        self.assertEqual(self.step(), [])
        self.assertEqual(self.dm.failures, {})
        self.world["sig"] = (2, 2)  # `claude auth login` rewrote the credentials
        self.assertEqual(self.step(), ["a", "b"])
        self.assertIsNone(self.dm.held)

    def test_login_between_passes_is_not_mistaken_for_a_logout(self):
        self.assertEqual(self.step(), ["a", "b"])
        self.world["exit"] = ("auth", "Error: You must be logged in to use Remote Control.")
        self.world["sig"] = (2, 2)  # logged back in before the daemon noticed the exits
        self.assertEqual(self.step(), ["a", "b"])  # retried in the same pass, no hold, no back-off
        self.assertIsNone(self.dm.held)
        self.assertEqual(self.dm.failures, {})

    def test_outage_holds_and_recovery_restarts_without_backoff(self):
        self.step()
        self.world.update(exit=("network", "Error: Server unreachable for 11 minutes, giving up."), online=False)
        self.assertEqual(self.step(), [])
        self.assertIn("network down", self.dm.held)
        self.assertFalse(self.dm.preflight())
        self.world["online"] = True
        self.assertTrue(self.dm.preflight())
        self.assertEqual(self.step(), ["a", "b"])
        self.assertEqual(self.dm.failures, {})

    def test_environment_refusal_holds_until_restart(self):
        self.step()
        self.world["exit"] = ("env", "Error: Remote Control requires feature-flag evaluation")
        self.assertEqual(self.step(), [])
        self.assertIn("environment", self.dm.held)
        self.world["sig"] = (3, 3)
        self.assertEqual(self.step(), [])

    def test_crash_backs_off_per_folder_and_marks_failing(self):
        self.dm.cfg.failing_after = 3
        self.world["running"] = set()
        self.assertEqual(self.step(), ["a", "b"])
        self.world["running"] = {Path("/p/b")}  # b stays up; only a keeps crashing
        for n in (1, 2, 3):
            self.assertEqual(self.step(), [])
            self.assertEqual(self.dm.failures, {Path("/p/a"): n})
            if n < 3:
                self.dm.next_allowed.clear()  # skip the wait
                self.assertEqual(self.step(), ["a"])
        self.assertGreater(self.dm.next_allowed[Path("/p/a")] - time.monotonic(), d.FAILING_RETRY_SECONDS - 60)
        state = d.read_state(self.dm.cfg)
        self.assertEqual(state["projects"]["/p/a"]["failures"], 3)
        self.assertEqual(state["projects"]["/p/a"]["last_exit"], ["crash", "Error: boom"])
        self.assertNotIn("/p/b", state["projects"])


if __name__ == "__main__":
    unittest.main()
