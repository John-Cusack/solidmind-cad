"""Keyboard teleop mapping for Isaac UI runtimes.

This module intentionally has no hard dependency on Isaac/Omniverse imports so
it can be tested outside Isaac and embedded by bridge processes that do the
actual event subscription.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TeleopCommand:
    """Desired robot motion command from keyboard state."""

    vx_mps: float = 0.0
    yaw_rate_rps: float = 0.0
    body_height_m: float = 0.0


class KeyboardTeleopMapper:
    """Map pressed keys to velocity/turn/height commands.

    Default bindings:
    - W/S: forward/backward linear velocity
    - A/D: yaw left/right
    - Q/E: body height up/down trim
    """

    def __init__(
        self,
        linear_speed_mps: float = 0.3,
        yaw_speed_rps: float = 0.8,
        height_step_m: float = 0.01,
    ) -> None:
        self._linear_speed_mps = linear_speed_mps
        self._yaw_speed_rps = yaw_speed_rps
        self._height_step_m = height_step_m

    def from_pressed_keys(self, pressed: set[str], current_height_m: float = 0.0) -> TeleopCommand:
        """Compute command from currently pressed key symbols.

        Keys are expected as uppercase single-character strings.
        """
        vx = 0.0
        yaw = 0.0
        height = current_height_m

        if "W" in pressed:
            vx += self._linear_speed_mps
        if "S" in pressed:
            vx -= self._linear_speed_mps
        if "A" in pressed:
            yaw += self._yaw_speed_rps
        if "D" in pressed:
            yaw -= self._yaw_speed_rps
        if "Q" in pressed:
            height += self._height_step_m
        if "E" in pressed:
            height -= self._height_step_m

        return TeleopCommand(vx_mps=vx, yaw_rate_rps=yaw, body_height_m=height)
