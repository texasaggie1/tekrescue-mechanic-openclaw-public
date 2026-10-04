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


if __name__ == "__main__":
    unittest.main()
