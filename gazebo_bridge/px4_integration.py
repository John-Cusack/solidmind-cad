"""PX4 process lifecycle manager for Gazebo bridge phase-3 integration."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any


class Px4Error(Exception):
    """Structured PX4 lifecycle error."""

    def __init__(self, message: str, *, code: str = "GAZEBO_PX4_NOT_READY") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class Px4Status:
    running: bool
    mode: str
    pid: int | None
    uptime_s: float
    system_address: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "mode": self.mode,
            "pid": self.pid,
            "uptime_s": self.uptime_s,
            "system_address": self.system_address,
        }


class Px4Manager:
    """Manage a single PX4 SITL process for one Gazebo bridge runtime."""

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = bool(enabled)
        self._process: subprocess.Popen[str] | None = None
        self._started_at_s: float | None = None
        self._fake_running = False
        # Stored in pymavlink-compatible form so MavlinkController can use
        # it directly; the canonical PX4 SITL offboard endpoint is UDP 14540.
        self._system_address = "udp:127.0.0.1:14540"
        # ``external`` mode: PX4 is already running outside the bridge
        # (e.g. operator launched ``make px4_sitl gz_x500`` in another
        # terminal).  Px4Manager records the state without forking, and
        # downstream code (MavlinkController) connects to the live process.
        self._external_running = False

    def start(
        self,
        *,
        binary: str | None = None,
        args: list[str] | None = None,
        system_address: str | None = None,
    ) -> dict[str, Any]:
        if not self._enabled:
            raise Px4Error(
                "PX4 integration is disabled for this bridge process. Start bridge with --enable-px4.",
            )
        if self._is_running():
            return {"started": False, "status": self.status()}

        if system_address:
            self._system_address = str(system_address)

        if os.environ.get("SOLIDMIND_GAZEBO_PX4_FAKE", "") == "1":
            self._fake_running = True
            self._started_at_s = time.time()
            return {"started": True, "status": self.status()}

        # External mode: PX4 already running outside the bridge.  Useful
        # for dev workflows where ``make px4_sitl gz_x500`` is launched
        # manually and the bridge just needs to attach via MAVLink.
        if os.environ.get("SOLIDMIND_PX4_EXTERNAL", "") == "1":
            self._external_running = True
            self._started_at_s = time.time()
            if system_address:
                self._system_address = str(system_address)
            return {"started": True, "status": self.status()}

        cmd_binary = binary or os.environ.get("SOLIDMIND_PX4_BIN", "px4")
        resolved = shutil.which(cmd_binary)
        if resolved is None:
            raise Px4Error(
                f"PX4 binary '{cmd_binary}' not found on PATH. Install PX4 or set SOLIDMIND_PX4_BIN.",
            )

        cmd = [resolved]
        if args:
            cmd.extend(args)

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            raise Px4Error(f"Failed to launch PX4: {exc}") from exc

        self._started_at_s = time.time()
        return {"started": True, "status": self.status()}

    def status(self) -> dict[str, Any]:
        started = self._started_at_s or time.time()
        uptime_s = max(0.0, time.time() - started)
        running = self._is_running()
        pid = self._process.pid if (self._process is not None and running) else None
        if self._fake_running:
            mode = "fake"
        elif self._external_running:
            mode = "external"
        else:
            mode = "process"
        return Px4Status(
            running=running,
            mode=mode,
            pid=pid,
            uptime_s=uptime_s,
            system_address=self._system_address,
        ).to_dict()

    def stop(self) -> dict[str, Any]:
        if self._fake_running:
            self._fake_running = False
            self._started_at_s = None
            return {"stopped": True, "status": self.status()}

        if self._external_running:
            # We don't own the external process; just forget about it.
            self._external_running = False
            self._started_at_s = None
            return {"stopped": True, "status": self.status()}

        proc = self._process
        if proc is None:
            return {"stopped": False, "status": self.status()}

        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)

        self._process = None
        self._started_at_s = None
        return {"stopped": True, "status": self.status()}

    def _is_running(self) -> bool:
        if self._fake_running or self._external_running:
            return True
        if self._process is None:
            return False
        return self._process.poll() is None

    def is_fake_mode(self) -> bool:
        """Return True when running in stub mode (no real PX4 binary).

        ``external`` mode is NOT fake — the bridge should still attach a
        MavlinkController to the externally-running PX4.
        """
        return self._fake_running

    def get_mavlink_url(self) -> str:
        """Return the pymavlink-format endpoint for the running PX4 process.

        The Gazebo bridge's MavlinkController consumes this directly when
        attaching to a px4_offboard teleop session.
        """
        return self._system_address
