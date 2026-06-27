"""Controllers used by the Gazebo runtime teleop sessions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("solidmind.gazebo_controllers")


class ControllerError(Exception):
    """Raised when a controller cannot be created or used."""

    def __init__(self, message: str, *, code: str = "INVALID_INPUT") -> None:
        super().__init__(message)
        self.code = code


def _clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


@dataclass(slots=True)
class MultirotorDirectController:
    """Map vx/vy/vz/yaw_rate commands to deterministic 4-rotor setpoints."""

    thrust_bias: float = 0.55
    gain_vx: float = 0.25
    gain_vy: float = 0.25
    gain_vz: float = 0.30
    gain_yaw: float = 0.20

    def command_to_rotors(
        self,
        *,
        vx_mps: float,
        vy_mps: float,
        vz_mps: float,
        yaw_rate_rps: float,
    ) -> dict[str, float]:
        pitch = _clamp(vx_mps * self.gain_vx, -0.35, 0.35)
        roll = _clamp(vy_mps * self.gain_vy, -0.35, 0.35)
        yaw = _clamp(yaw_rate_rps * self.gain_yaw, -0.30, 0.30)
        thrust = _clamp(self.thrust_bias + (vz_mps * self.gain_vz), 0.10, 0.95)

        r0 = _clamp(thrust + pitch - roll + yaw, 0.0, 1.0)
        r1 = _clamp(thrust + pitch + roll - yaw, 0.0, 1.0)
        r2 = _clamp(thrust - pitch + roll + yaw, 0.0, 1.0)
        r3 = _clamp(thrust - pitch - roll - yaw, 0.0, 1.0)

        return {
            "rotor_front_left": r0,
            "rotor_front_right": r1,
            "rotor_rear_right": r2,
            "rotor_rear_left": r3,
        }


@dataclass(slots=True)
class Px4OffboardController:
    """Bridge between teleop commands and a PX4 MAVLink session.

    When ``mavlink`` is attached (real PX4 SITL run), ``command_to_setpoint``
    forwards the velocity to PX4 via ``MavlinkController.set_velocity``,
    which updates the streaming setpoint that PX4's offboard mode reads.

    When ``mavlink`` is None (stub mode, fake_running, unit tests),
    ``command_to_setpoint`` falls back to a pure echo so existing
    teleop-state tests keep passing without a real autopilot.

    The controller is constructed by ``create_controller`` without a
    MavlinkController; the runtime calls ``attach_mavlink`` after it has
    successfully connected to PX4.  This keeps the controller factory
    cheap and side-effect-free.
    """

    # Stored as Any to avoid a hard import dep on pymavlink at module load
    # time; runtime injects a real ``server.mavlink_controller.MavlinkController``.
    mavlink: Any | None = field(default=None)

    def attach_mavlink(self, mavlink: Any) -> None:
        """Wire a connected MavlinkController into this controller."""
        self.mavlink = mavlink

    def detach_mavlink(self) -> None:
        """Drop the MavlinkController reference (used during session teardown)."""
        self.mavlink = None

    def command_to_setpoint(
        self,
        *,
        vx_mps: float,
        vy_mps: float,
        vz_mps: float,
        yaw_rate_rps: float,
    ) -> dict[str, float]:
        if self.mavlink is not None:
            try:
                self.mavlink.set_velocity(
                    vx_mps=float(vx_mps),
                    vy_mps=float(vy_mps),
                    vz_mps=float(vz_mps),
                    yaw_rate_rps=float(yaw_rate_rps),
                )
            except Exception as exc:  # noqa: BLE001
                # A failed setpoint must not nuke the teleop session — the
                # autopilot will hold its last good setpoint.  Log so the
                # operator can see the issue, but return the echo dict
                # unchanged so tests + telemetry keep flowing.
                logger.warning(
                    "PX4 setpoint forwarding failed: %s (returning echo only)",
                    exc,
                )
        return {
            "vx_mps": float(vx_mps),
            "vy_mps": float(vy_mps),
            "vz_mps": float(vz_mps),
            "yaw_rate_rps": float(yaw_rate_rps),
        }


def create_controller(
    controller_type: str,
    profile: dict[str, Any] | None = None,
) -> MultirotorDirectController | Px4OffboardController:
    """Construct a controller from profile settings."""
    controller = str(controller_type or "multirotor_direct").strip().lower()
    settings = profile or {}

    if controller == "multirotor_direct":
        return MultirotorDirectController(
            thrust_bias=float(settings.get("thrust_bias", 0.55)),
            gain_vx=float(settings.get("gain_vx", 0.25)),
            gain_vy=float(settings.get("gain_vy", 0.25)),
            gain_vz=float(settings.get("gain_vz", 0.30)),
            gain_yaw=float(settings.get("gain_yaw", 0.20)),
        )
    if controller == "px4_offboard":
        return Px4OffboardController()

    raise ControllerError(
        ("Gazebo profile.controller_type must be one of ['multirotor_direct', 'px4_offboard']"),
    )
