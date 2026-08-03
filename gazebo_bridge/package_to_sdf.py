"""Compile a canonical sim package into SDF — the Gazebo dialect lives here.

Core writes one neutral package (``manifest.json`` + meshes + a courtesy
URDF); every vendor dialect is compiled on the engine's own side of the
boundary (architecture doc, Principle 3).  This module is that compiler for
Gazebo: manifest in, SDF out, including the bits that are unmistakably
Gazebo's — ``gz-sim-multicopter-motor-model-system`` plugins, ``navsat`` and
``air_pressure`` sensors, mesh-vs-primitive collision choices.

It runs at **load time** (``spawn_model``/``simulate``/``teleop_start`` with a
``package_path``), not at export time, so the SDF an engine consumes is always
compiled by the version of the engine that consumes it.

Everything here reads the manifest and nothing else — no core imports, no
FreeCAD, and no knowledge of how the package was produced.
"""

from __future__ import annotations

import json
import math
import os
import xml.etree.ElementTree as ET
from typing import Any

__all__ = [
    "SdfCompileError",
    "compile_and_validate",
    "compile_package_to_sdf",
    "load_manifest",
    "manifest_to_sdf_tree",
    "validate_sdf",
]

SDF_VERSION = "1.10"
MANIFEST_FILENAME = "manifest.json"

# Motor-model defaults.  These are Gazebo tuning parameters, not physical
# properties of the drone, so they live engine-side and are never carried in
# the canonical manifest.
_DEFAULT_MOTOR_CONSTANT = 8.54858e-06
_DEFAULT_MOMENT_CONSTANT = 0.016
_DEFAULT_MAX_ROT_VELOCITY = 1000.0
_TIME_CONSTANT_UP = 0.0125
_TIME_CONSTANT_DOWN = 0.025
_ROTOR_DRAG_COEFFICIENT = "8.06428e-05"
_ROLLING_MOMENT_COEFFICIENT = "1e-06"
_ROTOR_VELOCITY_SLOWDOWN_SIM = "10"


class SdfCompileError(Exception):
    """Raised when a package cannot be compiled into SDF."""

    def __init__(self, message: str, *, code: str = "PACKAGE_INVALID") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt(val: float) -> str:
    """Format a float, stripping trailing zeros."""
    return f"{val:.6g}"


def _quat_to_rpy(quat: tuple[float, float, float, float]) -> tuple[float, float, float]:
    """Convert a (w, x, y, z) quaternion to roll-pitch-yaw radians."""
    w, x, y, z = quat
    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    sinp = 2.0 * (w * y - z * x)
    pitch = math.copysign(math.pi / 2.0, sinp) if abs(sinp) >= 1.0 else math.asin(sinp)

    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return (roll, pitch, yaw)


def _pose_str(xyz: list[float], quat: list[float]) -> str:
    roll, pitch, yaw = _quat_to_rpy((quat[0], quat[1], quat[2], quat[3]))
    return f"{_fmt(xyz[0])} {_fmt(xyz[1])} {_fmt(xyz[2])} {_fmt(roll)} {_fmt(pitch)} {_fmt(yaw)}"


def _scale_str(scale_to_m: float) -> str:
    return f"{_fmt(scale_to_m)} {_fmt(scale_to_m)} {_fmt(scale_to_m)}"


def _root_link_name(manifest: dict[str, Any]) -> str:
    """Name of the model's root link — the sensor and mixer reference frame."""
    links = manifest.get("links") or []
    for link in links:
        if link.get("is_root"):
            return str(link.get("name", ""))
    children = {j.get("child") for j in manifest.get("joints") or []}
    for link in links:
        if link.get("name") not in children:
            return str(link.get("name", ""))
    return str(links[0].get("name", "")) if links else ""


def load_manifest(package_path: str) -> tuple[dict[str, Any], str]:
    """Load ``manifest.json`` from a package directory (or a direct path).

    Returns ``(manifest, package_dir)`` — mesh references inside the manifest
    are relative to that directory.
    """
    if os.path.isdir(package_path):
        manifest_path = os.path.join(package_path, MANIFEST_FILENAME)
    else:
        manifest_path = package_path
    if not os.path.isfile(manifest_path):
        raise SdfCompileError(f"No sim package manifest at {manifest_path}")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SdfCompileError(f"Cannot read manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or not manifest.get("links"):
        raise SdfCompileError(f"Manifest {manifest_path} has no links")
    return manifest, os.path.dirname(os.path.abspath(manifest_path))


# ---------------------------------------------------------------------------
# Element emitters
# ---------------------------------------------------------------------------


def _emit_mesh_visual(link_el: ET.Element, name: str, uri: str, scale_to_m: float) -> None:
    visual = ET.SubElement(link_el, "visual", name=f"{name}_visual")
    ET.SubElement(visual, "pose").text = "0 0 0 0 0 0"
    geom = ET.SubElement(visual, "geometry")
    mesh = ET.SubElement(geom, "mesh")
    ET.SubElement(mesh, "uri").text = uri
    ET.SubElement(mesh, "scale").text = _scale_str(scale_to_m)


def _emit_mesh_collision(link_el: ET.Element, name: str, uri: str, scale_to_m: float) -> None:
    """Mesh-based collision — the fallback when a link declares no primitive.

    Gazebo Harmonic's DART/ODE can abort during contact resolution on complex
    meshes (multi-blade propellers are the classic case), so a link that knows
    its primitive shape should declare ``collision`` in the manifest instead.
    """
    coll = ET.SubElement(link_el, "collision", name=f"{name}_collision")
    ET.SubElement(coll, "pose").text = "0 0 0 0 0 0"
    geom = ET.SubElement(coll, "geometry")
    mesh = ET.SubElement(geom, "mesh")
    ET.SubElement(mesh, "uri").text = uri
    ET.SubElement(mesh, "scale").text = _scale_str(scale_to_m)


def _emit_primitive_geometry(parent: ET.Element, collision: dict[str, Any], name: str) -> None:
    geom = ET.SubElement(parent, "geometry")
    kind = collision.get("kind")
    if kind == "box":
        size = collision.get("size_m")
        if not size:
            raise SdfCompileError(f"link '{name}': box collision requires size_m")
        box = ET.SubElement(geom, "box")
        ET.SubElement(box, "size").text = f"{size[0]} {size[1]} {size[2]}"
    elif kind == "cylinder":
        radius, length = collision.get("radius_m"), collision.get("length_m")
        if radius is None or length is None:
            raise SdfCompileError(f"link '{name}': cylinder collision requires radius_m + length_m")
        cyl = ET.SubElement(geom, "cylinder")
        ET.SubElement(cyl, "radius").text = _fmt(float(radius))
        ET.SubElement(cyl, "length").text = _fmt(float(length))
    else:
        raise SdfCompileError(f"link '{name}': unsupported collision kind {kind!r}")


def _emit_primitive_collision(link_el: ET.Element, name: str, collision: dict[str, Any]) -> None:
    coll = ET.SubElement(link_el, "collision", name=f"{name}_collision")
    ET.SubElement(coll, "pose").text = "0 0 0 0 0 0"
    _emit_primitive_geometry(coll, collision, name)


def _emit_primitive_visual(link_el: ET.Element, name: str, collision: dict[str, Any]) -> None:
    """Visual matching a primitive collision, so mesh-less links still render.

    A procedurally-built drone (chassis + rotors from an airframe spec) has no
    meshes at all; without this it is invisible in the GUI even though physics
    is correct.  Rotors are tinted darker than the airframe so the two are
    tellable apart at a glance.
    """
    visual = ET.SubElement(link_el, "visual", name=f"{name}_visual")
    ET.SubElement(visual, "pose").text = "0 0 0 0 0 0"
    _emit_primitive_geometry(visual, collision, name)
    material = ET.SubElement(visual, "material")
    if "rotor" in name:
        ET.SubElement(material, "ambient").text = "0.2 0.2 0.2 1"
        ET.SubElement(material, "diffuse").text = "0.3 0.3 0.3 1"
    else:
        ET.SubElement(material, "ambient").text = "0.6 0.2 0.2 1"
        ET.SubElement(material, "diffuse").text = "0.8 0.3 0.3 1"


def _emit_inertial(link_el: ET.Element, link: dict[str, Any]) -> None:
    mass = link.get("mass_kg")
    if mass is None:
        return
    inertial = ET.SubElement(link_el, "inertial")
    ET.SubElement(inertial, "mass").text = _fmt(float(mass))
    tensor = link.get("inertia")
    if tensor:
        inertia = ET.SubElement(inertial, "inertia")
        for key in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz"):
            ET.SubElement(inertia, key).text = _fmt(float(tensor.get(key, 0.0)))


def _emit_multicopter_motor_plugin(
    model_el: ET.Element,
    *,
    joint_name: str,
    link_name: str,
    direction: str,
    actuator_number: int,
    motor_constant: float,
    moment_constant: float,
    max_rot_velocity: float,
) -> None:
    """One ``MulticopterMotorModel`` plugin per rotor (Gazebo canonical).

    The plugin must be one-per-rotor — not one plugin with several ``<rotor>``
    children, a shape PX4 and gz-sim reject.  Each reads
    ``velocity[actuator_number]`` from the ``gz.msgs.Actuators`` message PX4's
    ``gz_bridge`` publishes on ``/<model>/command/motor_speed``.
    """
    plugin = ET.SubElement(
        model_el,
        "plugin",
        filename="gz-sim-multicopter-motor-model-system",
        name="gz::sim::systems::MulticopterMotorModel",
    )
    # Deliberately no <robotNamespace>: gz-sim subscribes to
    # /model/<model_name>/<commandSubTopic> by default, which is exactly what
    # PX4 publishes.  Setting it breaks the topic match and the drone never
    # sees a motor command.
    ET.SubElement(plugin, "jointName").text = joint_name
    ET.SubElement(plugin, "linkName").text = link_name
    ET.SubElement(plugin, "turningDirection").text = direction
    ET.SubElement(plugin, "timeConstantUp").text = _fmt(_TIME_CONSTANT_UP)
    ET.SubElement(plugin, "timeConstantDown").text = _fmt(_TIME_CONSTANT_DOWN)
    ET.SubElement(plugin, "maxRotVelocity").text = _fmt(max_rot_velocity)
    ET.SubElement(plugin, "motorConstant").text = _fmt(motor_constant)
    ET.SubElement(plugin, "momentConstant").text = _fmt(moment_constant)
    ET.SubElement(plugin, "commandSubTopic").text = "command/motor_speed"
    ET.SubElement(plugin, "motorNumber").text = str(actuator_number)
    ET.SubElement(plugin, "rotorDragCoefficient").text = _ROTOR_DRAG_COEFFICIENT
    ET.SubElement(plugin, "rollingMomentCoefficient").text = _ROLLING_MOMENT_COEFFICIENT
    ET.SubElement(plugin, "motorSpeedPubTopic").text = f"motor_speed/{actuator_number}"
    ET.SubElement(plugin, "rotorVelocitySlowdownSim").text = _ROTOR_VELOCITY_SLOWDOWN_SIM
    # Required: PX4's gz_bridge sends velocity (rad/s) commands.  Without it
    # the plugin defaults to position/force control and never produces lift.
    ET.SubElement(plugin, "motorType").text = "velocity"


def _emit_imu_sensor(link_el: ET.Element) -> None:
    sensor = ET.SubElement(link_el, "sensor", name="imu_sensor", type="imu")
    ET.SubElement(sensor, "always_on").text = "1"
    ET.SubElement(sensor, "update_rate").text = "250"
    imu = ET.SubElement(sensor, "imu")
    for parent_tag, stddev, bias_mean, bias_stddev in (
        ("angular_velocity", 0.0001, 7.5e-06, 8.0e-07),
        ("linear_acceleration", 0.001, 1.0e-04, 1.0e-03),
    ):
        parent_el = ET.SubElement(imu, parent_tag)
        for axis in ("x", "y", "z"):
            axis_el = ET.SubElement(parent_el, axis)
            noise = ET.SubElement(axis_el, "noise", type="gaussian")
            ET.SubElement(noise, "mean").text = "0"
            ET.SubElement(noise, "stddev").text = _fmt(stddev)
            ET.SubElement(noise, "bias_mean").text = _fmt(bias_mean)
            ET.SubElement(noise, "bias_stddev").text = _fmt(bias_stddev)


def _emit_gps_sensor(link_el: ET.Element) -> None:
    sensor = ET.SubElement(link_el, "sensor", name="navsat_sensor", type="navsat")
    ET.SubElement(sensor, "always_on").text = "1"
    ET.SubElement(sensor, "update_rate").text = "20"


def _emit_barometer_sensor(link_el: ET.Element) -> None:
    sensor = ET.SubElement(link_el, "sensor", name="air_pressure_sensor", type="air_pressure")
    ET.SubElement(sensor, "always_on").text = "1"
    ET.SubElement(sensor, "update_rate").text = "50"
    air = ET.SubElement(sensor, "air_pressure")
    ET.SubElement(air, "reference_altitude").text = "0"
    pressure = ET.SubElement(air, "pressure")
    noise = ET.SubElement(pressure, "noise", type="gaussian")
    ET.SubElement(noise, "mean").text = "0"
    ET.SubElement(noise, "stddev").text = "1.0"


def _emit_magnetometer_sensor(link_el: ET.Element) -> None:
    sensor = ET.SubElement(link_el, "sensor", name="magnetometer_sensor", type="magnetometer")
    ET.SubElement(sensor, "always_on").text = "1"
    ET.SubElement(sensor, "update_rate").text = "50"
    mag = ET.SubElement(sensor, "magnetometer")
    for axis in ("x", "y", "z"):
        axis_el = ET.SubElement(mag, axis)
        noise = ET.SubElement(axis_el, "noise", type="gaussian")
        ET.SubElement(noise, "mean").text = "0"
        ET.SubElement(noise, "stddev").text = "1.0e-06"
        ET.SubElement(noise, "bias_mean").text = "1.0e-06"
        ET.SubElement(noise, "bias_stddev").text = "3.0e-07"


_SENSOR_EMITTERS = {
    "imu": _emit_imu_sensor,
    "gps": _emit_gps_sensor,
    "barometer": _emit_barometer_sensor,
    "magnetometer": _emit_magnetometer_sensor,
}


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


def manifest_to_sdf_tree(manifest: dict[str, Any], package_dir: str) -> ET.Element:
    """Build the SDF element tree for *manifest*.

    Mesh URIs are resolved to absolute paths against *package_dir* so Gazebo
    can always find them regardless of its resource path.
    """
    sdf = ET.Element("sdf", version=SDF_VERSION)
    model_el = ET.SubElement(sdf, "model", name=str(manifest.get("name", "model")))
    ET.SubElement(model_el, "static").text = "false"

    link_els: dict[str, ET.Element] = {}
    for link in manifest.get("links") or []:
        name = str(link.get("name", ""))
        link_el = ET.SubElement(model_el, "link", name=name)
        link_els[name] = link_el

        pose = link.get("world_pose") or {}
        ET.SubElement(link_el, "pose").text = _pose_str(
            pose.get("xyz_m", [0.0, 0.0, 0.0]),
            pose.get("quat_wxyz", [1.0, 0.0, 0.0, 0.0]),
        )

        mesh = link.get("mesh")
        collision = link.get("collision")
        if mesh:
            uri = os.path.abspath(os.path.join(package_dir, str(mesh.get("file", ""))))
            scale = float(mesh.get("scale_to_m", 1.0))
            _emit_mesh_visual(link_el, name, uri, scale)
            if collision:
                _emit_primitive_collision(link_el, name, collision)
            else:
                _emit_mesh_collision(link_el, name, uri, scale)
        elif collision:
            _emit_primitive_visual(link_el, name, collision)
            _emit_primitive_collision(link_el, name, collision)

        _emit_inertial(link_el, link)

    for joint in manifest.get("joints") or []:
        joint_type = str(joint.get("type", "fixed"))
        # SDF has no 'continuous'; an unlimited revolute is the equivalent.
        sdf_type = "revolute" if joint_type == "continuous" else joint_type
        joint_el = ET.SubElement(model_el, "joint", name=str(joint.get("name", "")), type=sdf_type)
        ET.SubElement(joint_el, "parent").text = str(joint.get("parent", ""))
        ET.SubElement(joint_el, "child").text = str(joint.get("child", ""))
        # No <pose>: in SDF 1.10 the joint frame defaults to the child link's
        # frame, and the child link is already placed at its world pose.
        # Writing the origin again double-offsets the axis — a rotor would
        # visibly spin about a point away from its hub.
        axis_el = ET.SubElement(joint_el, "axis")
        axis = joint.get("axis") or [0.0, 0.0, 1.0]
        ET.SubElement(axis_el, "xyz").text = f"{_fmt(axis[0])} {_fmt(axis[1])} {_fmt(axis[2])}"
        limits = joint.get("limits")
        if limits:
            limit_el = ET.SubElement(axis_el, "limit")
            ET.SubElement(limit_el, "lower").text = _fmt(float(limits.get("lower", 0.0)))
            ET.SubElement(limit_el, "upper").text = _fmt(float(limits.get("upper", 0.0)))
            ET.SubElement(limit_el, "effort").text = _fmt(float(joint.get("effort", 100.0)))
            ET.SubElement(limit_el, "velocity").text = _fmt(float(joint.get("velocity", 10.0)))
        if sdf_type in ("revolute", "prismatic"):
            dynamics = ET.SubElement(axis_el, "dynamics")
            ET.SubElement(dynamics, "damping").text = _fmt(float(joint.get("damping", 0.1)))
            ET.SubElement(dynamics, "friction").text = _fmt(float(joint.get("friction", 0.0)))

    _emit_actuators(model_el, manifest)
    _emit_sensors(link_els, manifest)
    return sdf


def _emit_actuators(model_el: ET.Element, manifest: dict[str, Any]) -> None:
    joint_to_child = {j.get("name"): j.get("child") for j in manifest.get("joints") or []}
    for idx, actuator in enumerate(manifest.get("actuators") or []):
        if actuator.get("type") != "rotor":
            continue  # servo actuators have no Gazebo plugin equivalent yet
        joint_name = str(actuator.get("joint") or f"rotor_{idx}_joint")
        link_name = str(actuator.get("link") or joint_to_child.get(joint_name) or "")
        if not link_name:
            # Nothing to bind the plugin to — skip rather than emit a block
            # PX4 and gz-sim would reject.
            continue
        max_rot_velocity = float(
            actuator.get("max_rot_velocity_rad_s", _DEFAULT_MAX_ROT_VELOCITY),
        )
        motor_constant = actuator.get("motor_constant")
        if motor_constant is None:
            # Recover k from the abstract spec when the producer only stated
            # thrust: F = k·ω²  →  k = F / ω².
            max_thrust = actuator.get("max_thrust_N")
            motor_constant = (
                float(max_thrust) / (max_rot_velocity**2)
                if max_thrust and max_rot_velocity > 0
                else _DEFAULT_MOTOR_CONSTANT
            )
        _emit_multicopter_motor_plugin(
            model_el,
            joint_name=joint_name,
            link_name=link_name,
            direction="cw" if actuator.get("direction") == "cw" else "ccw",
            actuator_number=int(actuator.get("index", idx)),
            motor_constant=float(motor_constant),
            moment_constant=float(actuator.get("moment_constant", _DEFAULT_MOMENT_CONSTANT)),
            max_rot_velocity=max_rot_velocity,
        )


def _emit_sensors(link_els: dict[str, ET.Element], manifest: dict[str, Any]) -> None:
    default_link = _root_link_name(manifest)
    for sensor in manifest.get("sensors") or []:
        emitter = _SENSOR_EMITTERS.get(str(sensor.get("type", "")))
        if emitter is None:
            continue
        link_el = link_els.get(str(sensor.get("link") or default_link))
        if link_el is not None:
            emitter(link_el)


def compile_package_to_sdf(
    package_path: str,
    output_path: str | None = None,
) -> str:
    """Compile the package at *package_path* into an SDF file.

    Writes ``<package_dir>/<model_name>.sdf`` unless *output_path* is given,
    and returns the absolute path written.
    """
    manifest, package_dir = load_manifest(package_path)
    sdf = manifest_to_sdf_tree(manifest, package_dir)

    if output_path is None:
        output_path = os.path.join(package_dir, f"{manifest.get('name', 'model')}.sdf")
    tree = ET.ElementTree(sdf)
    ET.indent(tree, space="  ")
    out = os.path.abspath(output_path)
    try:
        tree.write(out, encoding="unicode", xml_declaration=True)
    except OSError as exc:
        raise SdfCompileError(f"Cannot write SDF to {out}: {exc}", code="ENGINE_ERROR") from exc
    return out


def compile_and_validate(
    package_path: str,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Compile a package and validate the result in one step.

    Returns ``{sdf_path, model_name, drone_mode, findings}``.  ``drone_mode``
    is true when the manifest declares rotor actuators, which turns on the
    drone-specific SDF checks.
    """
    manifest, package_dir = load_manifest(package_path)
    sdf = manifest_to_sdf_tree(manifest, package_dir)

    if output_path is None:
        output_path = os.path.join(package_dir, f"{manifest.get('name', 'model')}.sdf")
    tree = ET.ElementTree(sdf)
    ET.indent(tree, space="  ")
    out = os.path.abspath(output_path)
    try:
        tree.write(out, encoding="unicode", xml_declaration=True)
    except OSError as exc:
        raise SdfCompileError(f"Cannot write SDF to {out}: {exc}", code="ENGINE_ERROR") from exc

    drone_mode = any(
        a.get("type") == "rotor" for a in (manifest.get("actuators") or []) if isinstance(a, dict)
    )
    return {
        "sdf_path": out,
        "model_name": str(manifest.get("name", "model")),
        "drone_mode": drone_mode,
        "findings": validate_sdf(out, drone_mode=drone_mode),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _finding(rule_id: str, severity: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"rule_id": rule_id, "severity": severity, "message": message, **extra}


def validate_sdf(path: str, *, drone_mode: bool = False) -> list[dict[str, Any]]:
    """Structural checks on a compiled SDF.

    Findings are plain dicts — they cross the socket as data, so the engine
    never needs core's ``Finding`` type.
    """
    findings: list[dict[str, Any]] = []
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        return [_finding("sdf.parse_error", "block", f"SDF parse error: {exc}")]

    root = tree.getroot()
    if root.tag != "sdf":
        return [_finding("sdf.root_tag", "block", f"Root element is <{root.tag}>, expected <sdf>")]

    model_el = root.find("model")
    if model_el is None:
        return [_finding("sdf.model_missing", "block", "SDF is missing <model> element.")]

    links = model_el.findall("link")
    joints = model_el.findall("joint")
    if not links:
        return [_finding("sdf.links_missing", "block", "SDF model has no <link> elements.")]

    link_names = {lk.attrib.get("name", "") for lk in links}
    if len(links) > 1 and not joints:
        findings.append(
            _finding("sdf.joints_missing", "warn", "Model has multiple links but no joints.")
        )

    for jel in joints:
        jname = jel.attrib.get("name", "?")
        parent = (jel.findtext("parent") or "").strip()
        child = (jel.findtext("child") or "").strip()
        if not parent or parent not in link_names:
            findings.append(
                _finding(
                    "sdf.dangling_parent",
                    "block",
                    f"Joint '{jname}' references unknown parent '{parent}'.",
                    field=f"joint/{jname}/parent",
                )
            )
        if not child or child not in link_names:
            findings.append(
                _finding(
                    "sdf.dangling_child",
                    "block",
                    f"Joint '{jname}' references unknown child '{child}'.",
                    field=f"joint/{jname}/child",
                )
            )

    if drone_mode:
        if not model_el.findall("plugin"):
            findings.append(
                _finding(
                    "sdf.drone.plugin_missing",
                    "warn",
                    "Drone-mode SDF has no <plugin> control block.",
                )
            )
        if len(links) < 2:
            findings.append(
                _finding(
                    "sdf.drone.too_few_links",
                    "warn",
                    "Drone-mode SDF should include at least 2 links.",
                )
            )

    return findings
