"""Process lifecycle for PX4 SITL + Gazebo.

PX4 does not own its simulator, and neither owns the other's lifetime:

- PX4's ``px4-rc.gzsim`` starts ``gz sim -r -s`` as a background child of
  a transient ``/bin/sh`` that exits immediately, so the Gazebo server is
  reparented to init and **outlives PX4**.  Nothing in PX4 ever kills it.
- The next PX4 launch then *silently reuses* that world: ``px4-rc.gzsim``
  greps ``gz topic -l`` for the first ``/world/*/clock`` and, if it finds
  one, attaches to it and overwrites ``PX4_GZ_WORLD``.

That reuse is not benign.  ``gz_env.sh`` is sourced only on the branch
that starts a *new* server, and PX4's ``default.sdf`` carries no
``<plugin>`` tags at all — every sensor system is injected through
``GZ_SIM_SERVER_CONFIG_PATH``.  So a world left behind by a killed PX4,
or started without that environment, can have no IMU/GPS/baro at all.
PX4 attaches happily, spawns the model, and never receives a sample.

Killing the process group is not enough either: the Gazebo server is not
in it.  These helpers find the processes by name and by their own
session, which is why they exist rather than a plain ``killpg``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

# PX4 leaves these behind when it dies without running its shutdown path;
# a stale socket makes the next instance pick a different instance id.
PX4_STALE_FILES = ("/tmp/px4-sock-0", "/tmp/px4_lock-0")

# Matched against full command lines.  Order matters: PX4 first so it
# stops driving the world before the world goes away.
_PX4_PATTERNS = ("make px4_sitl_default", "build/px4_sitl_default/bin/px4")
_GAZEBO_PATTERNS = ("gz-sim-gui-client", "gz-sim-main")


def _ps_lines() -> list[tuple[int, str]]:
    """Every process as ``(pid, command_line)``, excluding ourselves.

    Deliberately not ``pgrep -f``: that matches the caller's own command
    line whenever the pattern appears in it, which has already produced a
    false "engine already listening" and a kill that hit nothing on this
    project.  Reading ``ps`` and dropping our own PID is explicit about it.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []

    mine = {os.getpid(), os.getppid()}
    rows: list[tuple[int, str]] = []
    for line in out.stdout.splitlines():
        pid_text, _, args = line.strip().partition(" ")
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid not in mine:
            rows.append((pid, args))
    return rows


def find_processes(pattern: str) -> list[int]:
    """PIDs whose command line contains ``pattern``, excluding ourselves."""
    return [pid for pid, args in _ps_lines() if pattern in args]


def gazebo_server_pids() -> list[int]:
    """The Gazebo *server* — the process that outlives PX4.

    ``gz-sim-main`` is also the GUI's binary (``-g``), so match the ``-s``
    that distinguishes the server; both still get reaped by
    ``stop_simulation``, but only the server is what PX4 attaches to.
    """
    return [pid for pid, args in _ps_lines() if "gz-sim-main" in args and " -s" in args]


def px4_pids() -> list[int]:
    pids: list[int] = []
    for pattern in _PX4_PATTERNS:
        pids.extend(find_processes(pattern))
    return sorted(set(pids))


def running_world() -> str | None:
    """Name of a world already publishing a clock, or ``None``.

    This is the same probe PX4 uses to decide whether to reuse a world
    (``px4-rc.gzsim``), so it answers the question that actually matters:
    would a PX4 launched right now start its own server, or inherit this?
    """
    try:
        out = subprocess.run(
            ["gz", "topic", "-l"],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("/world/") and line.endswith("/clock"):
            return line[len("/world/") : -len("/clock")]
    return None


def _kill(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def stop_simulation(*, timeout_s: float = 10.0, verbose: bool = True) -> None:
    """Stop PX4 and Gazebo and clear PX4's stale files.

    Safe to call when nothing is running.  SIGTERM first so Gazebo can
    close its transport cleanly, then SIGKILL whatever is left.
    """
    targets = px4_pids() + [pid for pattern in _GAZEBO_PATTERNS for pid in find_processes(pattern)]
    targets = sorted(set(targets))

    if targets:
        if verbose:
            print(f"  stopping {len(targets)} PX4/Gazebo process(es): {targets}")
        _kill(targets, signal.SIGTERM)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not any(_alive(pid) for pid in targets):
                break
            time.sleep(0.2)

        survivors = [pid for pid in targets if _alive(pid)]
        if survivors:
            if verbose:
                print(f"  SIGKILL for {survivors}")
            _kill(survivors, signal.SIGKILL)
            time.sleep(0.5)

    for stale in PX4_STALE_FILES:
        try:
            Path(stale).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ensure_clean_world(*, reap: bool = True, verbose: bool = True) -> None:
    """Guarantee PX4 will start its own Gazebo server rather than inherit one.

    Raises ``RuntimeError`` when a world is already running and ``reap``
    is False, because inheriting one silently is how a run ends up flying
    a model with no sensors in a world someone else configured.
    """
    world = running_world()
    stale_server = gazebo_server_pids()
    if world is None and not stale_server:
        return

    detail = f"world {world!r}" if world else f"server pid(s) {stale_server}"
    if not reap:
        raise RuntimeError(
            f"A Gazebo server is already running ({detail}). PX4 would attach "
            f"to it instead of starting its own, and a world it did not start "
            f"may have no sensor plugins loaded. Stop it first:\n"
            f"    python3 examples/quadrotor_camera_drone/flight_lab.py stop"
        )

    if verbose:
        print(f"  Found a pre-existing Gazebo {detail} — reaping it first.")
        print("  (PX4 would otherwise attach to it and spawn a second model.)")
    stop_simulation(verbose=verbose)
