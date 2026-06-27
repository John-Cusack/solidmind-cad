#!/usr/bin/env python3
"""
Hexapod leg segment position calculator.

Computes body positions and orientations for a 6-legged robot with:
- Circular chassis (75mm radius, 5mm thick)
- Coxa servo + arm (52mm)
- Femur servo + arm (66mm)
- Tibia servo + arm (133mm)

Outputs JSON-like structure for cad.create_primitives.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass
class Position:
    """3D position [x, y, z] in mm."""

    x: float
    y: float
    z: float

    def to_list(self) -> list[float]:
        return [self.x, self.y, self.z]


@dataclass
class Segment:
    """Represents a body primitive to create."""

    name: str
    shape: str
    dimensions: dict[str, float]
    position: list[float]
    rotation_angle_deg: float
    rotation_axis: list[float] = None

    def __post_init__(self):
        if self.rotation_axis is None:
            self.rotation_axis = [0, 0, 1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "shape": self.shape,
            "dimensions": self.dimensions,
            "position": self.position,
            "rotation_angle_deg": self.rotation_angle_deg,
            "rotation_axis": self.rotation_axis,
        }


class HexapodCalculator:
    """Calculates positions for all hexapod leg segments."""

    # Chassis parameters
    CHASSIS_RADIUS = 75  # mm
    CHASSIS_HEIGHT = 5  # mm
    CHASSIS_Z_MIN = 0
    CHASSIS_Z_MID = CHASSIS_HEIGHT / 2

    # Servo dimensions (SG90-like micro servo)
    SERVO_WIDTH = 23  # mm (X)
    SERVO_DEPTH = 12.2  # mm (Y)
    SERVO_HEIGHT = 22  # mm (Z)
    SERVO_RADIUS = SERVO_HEIGHT / 2  # Use half-height for centering

    # Coxa parameters
    COXA_SERVO_RADIUS = 55  # mm from center
    COXA_SERVO_Z = 2  # mm (bottom of pocket, centered vertically adds half servo height)
    COXA_ARM_LENGTH = 52  # mm
    COXA_ARM_WIDTH = 10  # mm
    COXA_ARM_HEIGHT = 5  # mm

    # Femur parameters
    FEMUR_ARM_LENGTH = 66  # mm
    FEMUR_ARM_WIDTH = 8  # mm
    FEMUR_ARM_HEIGHT = 5  # mm
    FEMUR_PITCH_DEG = 30  # degrees below horizontal

    # Tibia parameters
    TIBIA_ARM_LENGTH = 133  # mm
    TIBIA_ARM_WIDTH = 6  # mm
    TIBIA_ARM_HEIGHT = 4  # mm
    TIBIA_PITCH_DEG = 70  # degrees below horizontal (absolute)

    # Leg angles (6 legs around Z axis)
    LEG_ANGLES_DEG = [0, 60, 120, 180, 240, 300]

    def __init__(self):
        self.segments: list[Segment] = []

    def add_segment(self, segment: Segment) -> None:
        """Add a segment to the list."""
        self.segments.append(segment)

    def rad(self, deg: float) -> float:
        """Convert degrees to radians."""
        return math.radians(deg)

    def compute_all_legs(self) -> None:
        """Compute all leg positions for all 6 legs."""
        for leg_idx, leg_angle_deg in enumerate(self.LEG_ANGLES_DEG):
            self.compute_leg(leg_idx, leg_angle_deg)

    def compute_leg(self, leg_idx: int, leg_angle_deg: float) -> None:
        """Compute all segments for a single leg."""
        leg_angle_rad = self.rad(leg_angle_deg)
        cos_a = math.cos(leg_angle_rad)
        sin_a = math.sin(leg_angle_rad)

        # ==================== COXA SERVO ====================
        # Centered at radius 55mm, z=2 (pocket center)
        coxa_servo_pos = Position(
            x=self.COXA_SERVO_RADIUS * cos_a,
            y=self.COXA_SERVO_RADIUS * sin_a,
            z=self.COXA_SERVO_Z + self.SERVO_RADIUS,  # Center vertically
        )
        self.add_segment(
            Segment(
                name=f"coxa_servo_L{leg_idx + 1}",
                shape="box",
                dimensions={
                    "length": self.SERVO_WIDTH,
                    "width": self.SERVO_DEPTH,
                    "height": self.SERVO_HEIGHT,
                },
                position=coxa_servo_pos.to_list(),
                rotation_angle_deg=leg_angle_deg,
                rotation_axis=[0, 0, 1],
            )
        )

        # ==================== COXA ARM ====================
        # Starts at chassis edge (r=75mm), extends radially outward
        # Center of coxa arm is at radius = 75 + 52/2 = 101mm, z = 2.5
        coxa_arm_radius = self.CHASSIS_RADIUS + self.COXA_ARM_LENGTH / 2
        coxa_arm_pos = Position(
            x=coxa_arm_radius * cos_a,
            y=coxa_arm_radius * sin_a,
            z=self.CHASSIS_Z_MID,
        )
        self.add_segment(
            Segment(
                name=f"coxa_arm_L{leg_idx + 1}",
                shape="box",
                dimensions={
                    "length": self.COXA_ARM_LENGTH,
                    "width": self.COXA_ARM_WIDTH,
                    "height": self.COXA_ARM_HEIGHT,
                },
                position=coxa_arm_pos.to_list(),
                rotation_angle_deg=leg_angle_deg,
                rotation_axis=[0, 0, 1],
            )
        )

        # ==================== FEMUR SERVO ====================
        # At end of coxa arm (r = 75 + 52 = 127mm), same height as coxa end
        femur_servo_radius = self.CHASSIS_RADIUS + self.COXA_ARM_LENGTH
        femur_servo_z = self.CHASSIS_Z_MID  # Same height as coxa arm end
        femur_servo_pos = Position(
            x=femur_servo_radius * cos_a,
            y=femur_servo_radius * sin_a,
            z=femur_servo_z + self.SERVO_RADIUS,  # Center vertically
        )
        self.add_segment(
            Segment(
                name=f"femur_servo_L{leg_idx + 1}",
                shape="box",
                dimensions={
                    "length": self.SERVO_WIDTH,
                    "width": self.SERVO_DEPTH,
                    "height": self.SERVO_HEIGHT,
                },
                position=femur_servo_pos.to_list(),
                rotation_angle_deg=leg_angle_deg,
                rotation_axis=[0, 0, 1],
            )
        )

        # ==================== FEMUR ARM ====================
        # Starts at femur servo, extends 66mm at 30° below horizontal
        # The arm extends radially outward (along leg angle) and downward (pitch)

        # Half-length of femur arm
        femur_half_len = self.FEMUR_ARM_LENGTH / 2

        # Radial component (along leg_angle direction)
        femur_radial_offset = femur_half_len * math.cos(self.rad(self.FEMUR_PITCH_DEG))
        # Vertical component (downward is negative Z)
        femur_vertical_offset = -femur_half_len * math.sin(self.rad(self.FEMUR_PITCH_DEG))

        femur_arm_pos = Position(
            x=femur_servo_radius * cos_a + femur_radial_offset * cos_a,
            y=femur_servo_radius * sin_a + femur_radial_offset * sin_a,
            z=femur_servo_z + femur_vertical_offset,
        )

        # For rotation: yaw (leg angle) and pitch (femur pitch angle)
        # We rotate around Z by leg_angle, then around the local Y by femur_pitch
        # When rotation_axis is [0, 1, 0], rotation_angle_deg is pitch
        # But we can't express both rotations with a single axis/angle in this API
        # Strategy: we need to compute the effective single-axis rotation
        # For now, encode both: rotate around an axis in the XY plane at leg_angle, then pitch
        # Actually, the simpler approach: rotate around leg's local frame
        # Rotation order: first yaw (leg_angle) around Z, then pitch around rotated Y
        # Result: can use rotation_axis that's at leg_angle in XY plane
        femur_rotation_axis = [
            -math.sin(self.rad(leg_angle_deg)),  # Perpendicular to leg, in XY plane
            math.cos(self.rad(leg_angle_deg)),
            0,
        ]

        self.add_segment(
            Segment(
                name=f"femur_arm_L{leg_idx + 1}",
                shape="box",
                dimensions={
                    "length": self.FEMUR_ARM_LENGTH,
                    "width": self.FEMUR_ARM_WIDTH,
                    "height": self.FEMUR_ARM_HEIGHT,
                },
                position=femur_arm_pos.to_list(),
                rotation_angle_deg=self.FEMUR_PITCH_DEG,
                rotation_axis=femur_rotation_axis,
            )
        )

        # ==================== TIBIA SERVO ====================
        # At end of femur arm
        tibia_servo_pos = Position(
            x=femur_arm_pos.x + femur_half_len * math.cos(self.rad(self.FEMUR_PITCH_DEG)) * cos_a,
            y=femur_arm_pos.y + femur_half_len * math.cos(self.rad(self.FEMUR_PITCH_DEG)) * sin_a,
            z=femur_arm_pos.z - femur_half_len * math.sin(self.rad(self.FEMUR_PITCH_DEG)),
        )
        self.add_segment(
            Segment(
                name=f"tibia_servo_L{leg_idx + 1}",
                shape="box",
                dimensions={
                    "length": self.SERVO_WIDTH,
                    "width": self.SERVO_DEPTH,
                    "height": self.SERVO_HEIGHT,
                },
                position=tibia_servo_pos.to_list(),
                rotation_angle_deg=leg_angle_deg,
                rotation_axis=[0, 0, 1],
            )
        )

        # ==================== TIBIA ARM ====================
        # Starts at tibia servo, extends 133mm at 70° below horizontal (absolute)
        tibia_half_len = self.TIBIA_ARM_LENGTH / 2

        # Absolute pitch: 70° below horizontal
        tibia_radial_offset = tibia_half_len * math.cos(self.rad(self.TIBIA_PITCH_DEG))
        tibia_vertical_offset = -tibia_half_len * math.sin(self.rad(self.TIBIA_PITCH_DEG))

        tibia_arm_pos = Position(
            x=tibia_servo_pos.x + tibia_radial_offset * cos_a,
            y=tibia_servo_pos.y + tibia_radial_offset * sin_a,
            z=tibia_servo_pos.z + tibia_vertical_offset,
        )

        # Rotation axis perpendicular to leg, in XY plane
        tibia_rotation_axis = [
            -math.sin(self.rad(leg_angle_deg)),
            math.cos(self.rad(leg_angle_deg)),
            0,
        ]

        self.add_segment(
            Segment(
                name=f"tibia_arm_L{leg_idx + 1}",
                shape="box",
                dimensions={
                    "length": self.TIBIA_ARM_LENGTH,
                    "width": self.TIBIA_ARM_WIDTH,
                    "height": self.TIBIA_ARM_HEIGHT,
                },
                position=tibia_arm_pos.to_list(),
                rotation_angle_deg=self.TIBIA_PITCH_DEG,
                rotation_axis=tibia_rotation_axis,
            )
        )


def main():
    """Calculate and print hexapod leg positions."""
    calc = HexapodCalculator()
    calc.compute_all_legs()

    # Prepare output as JSON
    output = {
        "chassis": {
            "name": "chassis",
            "shape": "cylinder",
            "dimensions": {
                "radius": calc.CHASSIS_RADIUS,
                "height": calc.CHASSIS_HEIGHT,
            },
            "position": [0, 0, calc.CHASSIS_Z_MID],
            "rotation_angle_deg": 0,
            "rotation_axis": [0, 0, 1],
        },
        "legs": [seg.to_dict() for seg in calc.segments],
    }

    # Print as formatted JSON
    print(json.dumps(output, indent=2))

    # Also print as Python list suitable for cad.create_primitives
    print("\n\n# Python list for cad.create_primitives:")
    print("items = [")
    for seg in calc.segments:
        d = seg.to_dict()
        print("    {")
        print(f'        "name": "{d["name"]}", ')
        print(f'        "shape": "{d["shape"]}", ')
        print(f'        "dimensions": {d["dimensions"]}, ')
        print(f'        "position": {d["position"]}, ')
        print(f'        "rotation_angle_deg": {d["rotation_angle_deg"]}, ')
        print(f'        "rotation_axis": {d["rotation_axis"]}, ')
        print("    },")
    print("]")

    # Print a summary table
    print("\n\n# Summary table:")
    print(f"{'Segment':<25} {'Position [x, y, z]':<50} {'Rotation':<30}")
    print("-" * 105)
    for seg in calc.segments:
        pos = seg.position
        rot = f"{seg.rotation_angle_deg}° around {seg.rotation_axis}"
        print(f"{seg.name:<25} {str(pos):<50} {rot:<30}")


if __name__ == "__main__":
    main()
