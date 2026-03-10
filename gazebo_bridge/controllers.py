"""Controllers used by the Gazebo runtime teleop sessions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    """Pass-through high-level command envelope for PX4 offboard control."""

    def command_to_setpoint(
        self,
        *,
        vx_mps: float,
        vy_mps: float,
        vz_mps: float,
        yaw_rate_rps: float,
    ) -> dict[str, float]:
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
        (
            "Gazebo profile.controller_type must be one of "
            "['multirotor_direct', 'px4_offboard']"
        ),
    )

