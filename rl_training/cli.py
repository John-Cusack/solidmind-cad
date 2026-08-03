"""Command-line face of the RL pipeline.

Core drives this pipeline the way it drives an engine: as a subprocess on
*its own* interpreter.  RL needs Isaac Lab, which lives in Isaac Sim's bundled
Python — importing it into core's venv was never going to work, and after the
split it isn't even on core's path.

Every command prints one JSON object on stdout, so the caller parses a result
instead of scraping logs.  Errors print JSON too, with a non-zero exit:

    {"ok": false, "error": {"code": "...", "message": "..."}}

Usage::

    $ISAAC_PYTHON -m rl_training.cli configure --urdf robot.urdf --output cfg.py
    $ISAAC_PYTHON -m rl_training.cli export --checkpoint-dir runs/x --output-dir runs/x/deployed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _fail(code: str, message: str) -> int:
    json.dump({"ok": False, "error": {"code": code, "message": message}}, sys.stdout)
    sys.stdout.write("\n")
    return 1


def _emit(payload: dict[str, Any]) -> int:
    json.dump({"ok": True, **payload}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _configure(args: argparse.Namespace) -> int:
    """URDF → analysis → Isaac Lab env config."""
    from rl_training.env_configurator import generate_env_config
    from rl_training.urdf_analyzer import analyze_urdf

    urdf_path = Path(args.urdf)
    if not urdf_path.is_file():
        return _fail("URDF_NOT_FOUND", f"URDF file not found: {urdf_path}")

    try:
        analysis = analyze_urdf(urdf_path)
    except Exception as exc:  # noqa: BLE001 — the message is the result
        return _fail("URDF_PARSE_FAILED", f"Failed to parse URDF: {exc}")

    output = Path(args.output) if args.output else Path(f"{analysis.robot_name}_env_config.py")
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        config_path = generate_env_config(
            analysis, str(urdf_path), str(output), num_envs=args.num_envs
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("ENV_CONFIG_FAILED", f"Failed to generate env config: {exc}")

    return _emit(
        {
            "config_path": str(config_path),
            "analysis": {
                "robot_name": analysis.robot_name,
                "morphology": analysis.morphology,
                "actuated_joints": list(analysis.actuated_joints),
                "num_joints": len(analysis.actuated_joints),
                "total_mass_kg": analysis.total_mass_kg,
                "standing_height_m": analysis.standing_height_m,
                "base_link": analysis.base_link,
                "foot_links": list(analysis.foot_links),
                "joint_limits": {k: list(v) for k, v in analysis.joint_limits.items()},
            },
        }
    )


def _export(args: argparse.Namespace) -> int:
    """Checkpoint → deployable policy."""
    from rl_training.export_policy import export_policy

    checkpoint_dir = Path(args.checkpoint_dir)
    if not checkpoint_dir.is_dir():
        return _fail("DIR_NOT_FOUND", f"Directory not found: {checkpoint_dir}")

    joint_names = list(args.joint_names or [])
    action_scale = args.action_scale

    # The trainer records what it trained; prefer that over anything guessed.
    training_config = checkpoint_dir / "training_config.json"
    if not joint_names and training_config.is_file():
        try:
            config = json.loads(training_config.read_text(encoding="utf-8"))
            joint_names = list(config.get("joint_names") or [])
            per_joint = config.get("action_scale_per_joint") or []
            if per_joint:
                action_scale = sum(per_joint) / len(per_joint)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    if not joint_names:
        return _fail(
            "JOINT_NAMES_NOT_FOUND",
            "Cannot resolve joint_names from training_config.json — pass --joint-names.",
        )

    output_dir = Path(args.output_dir) if args.output_dir else checkpoint_dir / "deployed"
    try:
        result = export_policy(
            checkpoint_dir,
            output_dir,
            joint_names=joint_names,
            action_scale=action_scale,
            alpha=args.alpha,
        )
    except FileNotFoundError as exc:
        return _fail("CHECKPOINT_NOT_FOUND", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail("EXPORT_FAILED", f"Policy export failed: {exc}")

    return _emit(dict(result))


def _analyze(args: argparse.Namespace) -> int:
    """URDF → analysis only, for callers that want the numbers without a config."""
    from rl_training.urdf_analyzer import analyze_urdf

    urdf_path = Path(args.urdf)
    if not urdf_path.is_file():
        return _fail("URDF_NOT_FOUND", f"URDF file not found: {urdf_path}")
    try:
        analysis = analyze_urdf(urdf_path)
    except Exception as exc:  # noqa: BLE001
        return _fail("URDF_PARSE_FAILED", f"Failed to parse URDF: {exc}")
    return _emit(
        {
            "robot_name": analysis.robot_name,
            "morphology": analysis.morphology,
            "actuated_joints": list(analysis.actuated_joints),
            "total_mass_kg": analysis.total_mass_kg,
            "standing_height_m": analysis.standing_height_m,
            "base_link": analysis.base_link,
            "foot_links": list(analysis.foot_links),
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rl_training.cli",
        description="RL pipeline commands. Every command prints one JSON object.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser("configure", help="URDF → Isaac Lab env config")
    configure.add_argument("--urdf", required=True)
    configure.add_argument("--output", default=None)
    configure.add_argument("--num-envs", type=int, default=4096)
    configure.set_defaults(func=_configure)

    analyze = sub.add_parser("analyze", help="URDF → morphology analysis")
    analyze.add_argument("--urdf", required=True)
    analyze.set_defaults(func=_analyze)

    export = sub.add_parser("export", help="Checkpoint → deployable policy")
    export.add_argument("--checkpoint-dir", required=True)
    export.add_argument("--output-dir", default=None)
    export.add_argument("--joint-names", nargs="*", default=None)
    export.add_argument("--action-scale", type=float, default=0.3)
    export.add_argument("--alpha", type=float, default=0.3)
    export.set_defaults(func=_export)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
