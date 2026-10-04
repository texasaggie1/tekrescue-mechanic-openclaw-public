"""Tests for gateway liveness, self-heal, and the operator-disabled guard."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from mechanic import daemon


PRINT_RUNNING = "ai.openclaw.gateway = {\n\tactive count = 1\n\tpid = 32399\n\tstate = running\n}\n"
PRINT_IDLE = "ai.openclaw.gateway = {\n\tactive count = 0\n\tstate = not running\n}\n"
DISABLED = 'disabled services = {\n\t"ai.openclaw.gateway" => disabled\n}\n'
ENABLED = 'disabled services = {\n\t"ai.openclaw.gateway" => enabled\n}\n'


def fake_run(script: dict[str, tuple[int, str]]):
    """Build a subprocess.run stand-in keyed on the launchctl verb."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        verb = cmd[1]
        code, out = script.get(verb, (1, ""))
        return mock.Mock(returncode=code, stdout=out, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


class LivenessTests(unittest.TestCase):
    def test_running_pid_parses_launchctl_print(self) -> None:
        with mock.patch("mechanic.daemon.subprocess.run", fake_run({"print": (0, PRINT_RUNNING)})):
            self.assertEqual(daemon.running_pid("ai.openclaw.gateway"), 32399)
            self.assertTrue(daemon.is_running("ai.openclaw.gateway"))
        with mock.patch("mechanic.daemon.subprocess.run", fake_run({"print": (0, PRINT_IDLE)})):
            self.assertIsNone(daemon.running_pid("ai.openclaw.gateway"))
        with mock.patch("mechanic.daemon.subprocess.run", fake_run({"print": (113, "")})):
            self.assertIsNone(daemon.running_pid("ai.openclaw.gateway"))

    def test_check_gateway_running_is_ok(self) -> None:
        with mock.patch("mechanic.daemon.subprocess.run", fake_run({"print": (0, PRINT_RUNNING)})):
            check = daemon.check_gateway("ai.openclaw.gateway")
        self.assertTrue(check.ok)
        self.assertEqual(check.summary(), "Gateway running (pid 32399).")


class SelfHealTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("mechanic.daemon.plist_path_for")
        self.plist_for = patcher.start()
        self.addCleanup(patcher.stop)
        self.plist = mock.Mock(spec=Path)
        self.plist.exists.return_value = True
        self.plist.__str__ = lambda self_: "/fake/ai.openclaw.gateway.plist"  # type: ignore[assignment]
        self.plist_for.return_value = self.plist

    def test_no_plist_means_no_gateway_and_is_fine(self) -> None:
        self.plist.exists.return_value = False
        with mock.patch("mechanic.daemon.subprocess.run", fake_run({"print": (113, "")})):
            check = daemon.check_gateway("ai.hermes.gateway")
        self.assertTrue(check.ok)
        self.assertFalse(check.installed)
        self.assertEqual(check.summary(), "No gateway service installed.")

    def test_dead_gateway_is_kickstarted_when_autoheal_is_on(self) -> None:
        # check_gateway probes once, ensure_running re-probes before acting,
        # then polls until the kickstarted service reports a pid.
        answers = iter([(0, PRINT_IDLE), (0, PRINT_IDLE), (0, PRINT_RUNNING)])

        def run(cmd, **kwargs):
            run.calls.append(list(cmd))
            verb = cmd[1]
            if verb == "print":
                code, out = next(answers)
                return mock.Mock(returncode=code, stdout=out, stderr="")
            if verb == "print-disabled":
                return mock.Mock(returncode=0, stdout=ENABLED, stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        run.calls = []  # type: ignore[attr-defined]

        with mock.patch("mechanic.daemon.subprocess.run", run), mock.patch("mechanic.daemon.time.sleep"):
            check = daemon.check_gateway("ai.openclaw.gateway", autoheal=True)
        self.assertTrue(check.ok)
        self.assertTrue(check.healed)
        self.assertIn("restarted it (now pid 32399)", check.summary())
        verbs = [c[1] for c in run.calls]
        self.assertIn("bootstrap", verbs)
        self.assertIn("kickstart", verbs)

    def test_dead_gateway_is_reported_not_healed_when_autoheal_is_off(self) -> None:
        with mock.patch("mechanic.daemon.subprocess.run", fake_run({"print": (0, PRINT_IDLE), "print-disabled": (0, ENABLED)})) as run:
            check = daemon.check_gateway("ai.hermes.gateway", autoheal=False)
        self.assertFalse(check.ok)
        self.assertTrue(check.was_down)
        self.assertIn("autoheal is off", check.summary())
        self.assertNotIn("kickstart", [c[1] for c in run.calls])

    def test_operator_disabled_gateway_is_never_healed(self) -> None:
        with mock.patch("mechanic.daemon.subprocess.run", fake_run({"print": (0, PRINT_IDLE), "print-disabled": (0, DISABLED)})) as run:
            check = daemon.check_gateway("ai.openclaw.gateway", autoheal=True)
        self.assertFalse(check.ok)
        self.assertIn("disabled by the operator", check.summary())
        self.assertNotIn("kickstart", [c[1] for c in run.calls])
        self.assertNotIn("bootstrap", [c[1] for c in run.calls])

    def test_ensure_running_refuses_a_disabled_service(self) -> None:
        with mock.patch("mechanic.daemon.subprocess.run", fake_run({"print": (0, PRINT_IDLE), "print-disabled": (0, DISABLED)})):
            with self.assertRaises(daemon.DaemonControlError):
                daemon.ensure_running(label="ai.openclaw.gateway")


PRINT_HERMES_RUNNING = "ai.hermes.gateway = {\n\tactive count = 1\n\tpid = 37096\n\tstate = running\n}\n"
PRINT_HERMES_IDLE = "ai.hermes.gateway = {\n\tactive count = 0\n\tstate = not running\n}\n"
NOT_FOUND = 'Could not find service "ai.hermes.gateway" in domain for user gui: 502\n'
HERMES_DISABLED = 'disabled services = {\n\t"ai.hermes.gateway" => disabled\n}\n'
NOTHING_DISABLED = "disabled services = {\n}\n"


def domain_run(by_target: dict[str, tuple[int, str]], managername: str = "Background"):
    """subprocess.run stand-in keyed on `verb target`, as launchctl sees it.

    Models the 2026-10-04 Mac mini: the Hermes gateway lives in user/<uid>
    (loaded over SSH) and gui/<uid> has never heard of it.
    """
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        verb = cmd[1]
        if verb == "managername":
            return mock.Mock(returncode=0, stdout=managername + "\n", stderr="")
        key = f"{verb} {cmd[2]}" if len(cmd) > 2 else verb
        code, out = by_target.get(key, (0, ""))
        return mock.Mock(returncode=code, stdout=out, stderr=out if code else "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


class LaunchdDomainTests(unittest.TestCase):
    """A service loaded from an SSH session lives in user/<uid>, not gui/<uid>."""

    def setUp(self) -> None:
        self.gui, self.user = daemon.user_domains()
        self.label = "ai.hermes.gateway"

    def test_running_service_in_the_user_domain_is_found(self) -> None:
        script = {
            f"print {self.gui}/{self.label}": (113, NOT_FOUND),
            f"print {self.user}/{self.label}": (0, PRINT_HERMES_RUNNING),
        }
        with mock.patch("mechanic.daemon.subprocess.run", domain_run(script)):
            self.assertEqual(daemon.find_service(self.label), (self.user, 37096))
            self.assertEqual(daemon.running_pid(self.label), 37096)
            check = daemon.check_gateway(self.label, autoheal=False)
        self.assertTrue(check.ok)
        self.assertEqual(check.summary(), "Gateway running (pid 37096).")

    def test_unknown_in_both_domains_is_not_running(self) -> None:
        script = {
            f"print {self.gui}/{self.label}": (113, NOT_FOUND),
            f"print {self.user}/{self.label}": (113, NOT_FOUND),
        }
        with mock.patch("mechanic.daemon.subprocess.run", domain_run(script)):
            self.assertEqual(daemon.find_service(self.label), (None, None))

    def test_disabled_in_either_domain_counts(self) -> None:
        script = {
            f"print-disabled {self.gui}": (0, NOTHING_DISABLED),
            f"print-disabled {self.user}": (0, HERMES_DISABLED),
        }
        with mock.patch("mechanic.daemon.subprocess.run", domain_run(script)):
            self.assertTrue(daemon.is_disabled(self.label))
        script = {
            f"print-disabled {self.gui}": (0, NOTHING_DISABLED),
            f"print-disabled {self.user}": (0, NOTHING_DISABLED),
        }
        with mock.patch("mechanic.daemon.subprocess.run", domain_run(script)):
            self.assertFalse(daemon.is_disabled(self.label))
        script = {
            f"print-disabled {self.gui}": (1, ""),
            f"print-disabled {self.user}": (1, ""),
        }
        with mock.patch("mechanic.daemon.subprocess.run", domain_run(script)):
            self.assertIsNone(daemon.is_disabled(self.label))

    def test_self_heal_kicks_the_domain_the_service_lives_in(self) -> None:
        answers = iter([(0, PRINT_HERMES_IDLE), (0, PRINT_HERMES_IDLE), (0, PRINT_HERMES_RUNNING)])

        def run(cmd, **kwargs):
            run.calls.append(list(cmd))
            verb = cmd[1]
            if verb == "print" and cmd[2] == f"{self.gui}/{self.label}":
                return mock.Mock(returncode=113, stdout="", stderr=NOT_FOUND)
            if verb == "print":
                code, out = next(answers)
                return mock.Mock(returncode=code, stdout=out, stderr="")
            if verb == "print-disabled":
                return mock.Mock(returncode=0, stdout=NOTHING_DISABLED, stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        run.calls = []  # type: ignore[attr-defined]

        plist = mock.Mock(spec=Path)
        plist.exists.return_value = True
        plist.__str__ = lambda self_: "/fake/ai.hermes.gateway.plist"  # type: ignore[assignment]
        with mock.patch("mechanic.daemon.plist_path_for", return_value=plist), \
                mock.patch("mechanic.daemon.subprocess.run", run), \
                mock.patch("mechanic.daemon.time.sleep"):
            check = daemon.check_gateway(self.label, autoheal=True)
        self.assertTrue(check.healed)
        self.assertEqual(check.pid, 37096)
        bootstrap = next(c for c in run.calls if c[1] == "bootstrap")
        kickstart = next(c for c in run.calls if c[1] == "kickstart")
        self.assertEqual(bootstrap[2], self.user)
        self.assertEqual(kickstart[-1], f"{self.user}/{self.label}")

    def test_default_domain_follows_the_session_manager(self) -> None:
        with mock.patch("mechanic.daemon.subprocess.run", domain_run({}, managername="Aqua")):
            self.assertEqual(daemon.default_domain(), self.gui)
        with mock.patch("mechanic.daemon.subprocess.run", domain_run({}, managername="Background")):
            self.assertEqual(daemon.default_domain(), self.user)


if __name__ == "__main__":
    unittest.main()
