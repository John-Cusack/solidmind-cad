"""Tests for the PX4/Gazebo process reaper.

The bug this module exists for: PX4 reparents its Gazebo server so that
no signal sent to PX4's process group can reach it, and the *next* PX4
launch silently attaches to whatever world it finds still running.  One
was observed free-running for 63 minutes holding two models from two
separate runs.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from examples.quadrotor_camera_drone import sim_processes


def _ps_output(rows: list[tuple[int, str]]) -> subprocess.CompletedProcess[str]:
    text = "".join(f"{pid} {args}\n" for pid, args in rows)
    return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=text, stderr="")


class TestProcessDiscovery(unittest.TestCase):
    def test_never_matches_its_own_command_line(self) -> None:
        """`pgrep -f` matched the reaper itself — twice, on this project.

        Once producing a false "engine already listening", once a kill
        that hit nothing.  The pattern appears in our own argv precisely
        when we are the tool looking for it.
        """
        rows = [
            (os.getpid(), "python3 flight_lab.py stop gz-sim-main"),
            (4242, "/usr/libexec/gz/sim10/gz-sim-main -r -s default.sdf"),
        ]
        with mock.patch.object(subprocess, "run", return_value=_ps_output(rows)):
            found = sim_processes.find_processes("gz-sim-main")
        self.assertEqual(found, [4242])

    def test_server_is_distinguished_from_the_gui(self) -> None:
        """Both are `gz-sim-main`; only the `-s` one is what PX4 attaches to."""
        rows = [
            (100, "/usr/libexec/gz/sim10/gz-sim-main --verbose=1 -r -s default.sdf"),
            (101, "/usr/libexec/gz/sim10/gz-sim-main -g"),
            (102, "/usr/libexec/gz/sim10/gz-sim-gui-client"),
        ]
        with mock.patch.object(subprocess, "run", return_value=_ps_output(rows)):
            self.assertEqual(sim_processes.gazebo_server_pids(), [100])
            # ...but the reaper still has to take all three down.
            self.assertEqual(sim_processes.find_processes("gz-sim-main"), [100, 101])

    def test_malformed_ps_lines_are_skipped(self) -> None:
        out = subprocess.CompletedProcess(
            args=["ps"], returncode=0, stdout="\nnot-a-pid args\n  \n77 gz-sim-main -s\n", stderr=""
        )
        with mock.patch.object(subprocess, "run", return_value=out):
            self.assertEqual(sim_processes.find_processes("gz-sim-main"), [77])

    def test_missing_ps_is_not_fatal(self) -> None:
        with mock.patch.object(subprocess, "run", side_effect=OSError("no ps")):
            self.assertEqual(sim_processes.find_processes("anything"), [])


class TestRunningWorld(unittest.TestCase):
    """Mirrors the probe PX4 itself uses to decide whether to reuse a world."""

    def _gz_topics(self, topics: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["gz"], returncode=0, stdout="\n".join(topics) + "\n", stderr=""
        )

    def test_reports_the_world_publishing_a_clock(self) -> None:
        out = self._gz_topics(["/stats", "/world/default/clock", "/world/default/pose/info"])
        with mock.patch.object(subprocess, "run", return_value=out):
            self.assertEqual(sim_processes.running_world(), "default")

    def test_no_world_when_nothing_publishes_a_clock(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=self._gz_topics(["/stats"])):
            self.assertIsNone(sim_processes.running_world())

    def test_missing_gz_binary_is_not_fatal(self) -> None:
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(sim_processes.running_world())


class TestEnsureCleanWorld(unittest.TestCase):
    def test_noop_when_nothing_is_running(self) -> None:
        with (
            mock.patch.object(sim_processes, "running_world", return_value=None),
            mock.patch.object(sim_processes, "gazebo_server_pids", return_value=[]),
            mock.patch.object(sim_processes, "stop_simulation") as stop,
        ):
            sim_processes.ensure_clean_world()
        stop.assert_not_called()

    def test_reaps_a_pre_existing_world(self) -> None:
        with (
            mock.patch.object(sim_processes, "running_world", return_value="default"),
            mock.patch.object(sim_processes, "gazebo_server_pids", return_value=[9]),
            mock.patch.object(sim_processes, "stop_simulation") as stop,
        ):
            sim_processes.ensure_clean_world(verbose=False)
        stop.assert_called_once()

    def test_refuses_rather_than_inheriting_when_reap_is_off(self) -> None:
        with (
            mock.patch.object(sim_processes, "running_world", return_value="default"),
            mock.patch.object(sim_processes, "gazebo_server_pids", return_value=[9]),
            self.assertRaises(RuntimeError) as cm,
        ):
            sim_processes.ensure_clean_world(reap=False)
        self.assertIn("already running", str(cm.exception))


class TestStopSimulation(unittest.TestCase):
    def test_safe_when_nothing_is_running(self) -> None:
        with (
            mock.patch.object(sim_processes, "px4_pids", return_value=[]),
            mock.patch.object(sim_processes, "find_processes", return_value=[]),
            mock.patch.object(sim_processes, "_kill") as kill,
        ):
            sim_processes.stop_simulation(verbose=False)
        kill.assert_not_called()

    def test_escalates_to_sigkill_for_survivors(self) -> None:
        import signal

        with (
            mock.patch.object(sim_processes, "px4_pids", return_value=[5]),
            mock.patch.object(sim_processes, "find_processes", return_value=[]),
            mock.patch.object(sim_processes, "_alive", return_value=True),
            mock.patch.object(sim_processes, "_kill") as kill,
            mock.patch.object(sim_processes.time, "sleep"),
            mock.patch.object(sim_processes.time, "monotonic", side_effect=[0.0, 99.0, 99.0]),
        ):
            sim_processes.stop_simulation(verbose=False)
        signals = [call.args[1] for call in kill.call_args_list]
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])

    def test_clears_px4_stale_files(self) -> None:
        """A stale socket makes the next PX4 pick a different instance id."""
        unlinked: list[str] = []

        class _FakePath:
            def __init__(self, p: str) -> None:
                self._p = p

            def unlink(self) -> None:
                unlinked.append(self._p)

        with (
            mock.patch.object(sim_processes, "px4_pids", return_value=[]),
            mock.patch.object(sim_processes, "find_processes", return_value=[]),
            mock.patch.object(sim_processes, "Path", _FakePath),
        ):
            sim_processes.stop_simulation(verbose=False)
        self.assertEqual(sorted(unlinked), sorted(sim_processes.PX4_STALE_FILES))


if __name__ == "__main__":
    unittest.main()
