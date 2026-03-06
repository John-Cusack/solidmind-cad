"""MCP tool implementations for the RL training pipeline.

Tools follow the same dispatch pattern as ``tools_motion.py`` and
``tools_study.py``.  Phase 1 uses subprocess management directly
(like ``study_runner.py``).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from server.urdf_analyzer import URDFAnalysis, analyze_urdf

log = logging.getLogger("solidmind.tools_rl")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Active training subprocesses: training_id → process info
_active_training: dict[str, dict[str, Any]] = {}


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


# ------------------------------------------------------------------
# rl.configure_environment
# ------------------------------------------------------------------

def rl_configure_environment(
    *,
    urdf_path: str,
    output_path: str | None = None,
    num_envs: int = 4096,
) -> dict[str, Any]:
    """Parse URDF → URDFAnalysis → generate Isaac Lab env config."""
    if not os.path.isfile(urdf_path):
        return _error_result("URDF_NOT_FOUND", f"URDF file not found: {urdf_path}")

    try:
        analysis = analyze_urdf(urdf_path)
    except Exception as exc:
        return _error_result("URDF_PARSE_FAILED", f"Failed to parse URDF: {exc}")

    # Default output path
    if output_path is None:
        output_dir = _PROJECT_ROOT / "training_runs"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / f"{analysis.robot_name}_env_config.py")

    try:
        from rl_training.env_configurator import generate_env_config
        config_path = generate_env_config(
            analysis, urdf_path, output_path, num_envs=num_envs,
        )
    except Exception as exc:
        return _error_result("ENV_CONFIG_FAILED", f"Failed to generate env config: {exc}")

    return {
        "ok": True,
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
            "joint_limits": {
                k: list(v) for k, v in analysis.joint_limits.items()
            },
        },
    }


# ------------------------------------------------------------------
# rl.start_training
# ------------------------------------------------------------------

def rl_start_training(
    *,
    env_config: str,
    output_dir: str | None = None,
    max_iterations: int | None = None,
    num_envs: int | None = None,
) -> dict[str, Any]:
    """Spawn training subprocess. Returns training_id."""
    if not os.path.isfile(env_config):
        return _error_result("CONFIG_NOT_FOUND", f"Env config not found: {env_config}")

    training_id = f"train_{uuid.uuid4().hex[:12]}"

    if output_dir is None:
        output_dir = str(_PROJECT_ROOT / "training_runs" / training_id)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Build command
    cmd = [
        sys.executable, "-m", "rl_training.train",
        "--env-config", env_config,
        "--output-dir", output_dir,
    ]
    if max_iterations is not None:
        cmd.extend(["--max-iterations", str(max_iterations)])
    if num_envs is not None:
        cmd.extend(["--num-envs", str(num_envs)])

    # Check for Isaac Lab Python override
    isaac_python = os.environ.get("ISAAC_PYTHON")
    if isaac_python and os.path.isfile(isaac_python):
        cmd[0] = isaac_python

    log.info("Starting training: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(_PROJECT_ROOT),
        )
    except Exception as exc:
        return _error_result("SPAWN_FAILED", f"Failed to spawn training: {exc}")

    _active_training[training_id] = {
        "process": proc,
        "pid": proc.pid,
        "output_dir": output_dir,
        "env_config": env_config,
        "started_at": time.time(),
    }

    return {
        "ok": True,
        "training_id": training_id,
        "pid": proc.pid,
        "output_dir": output_dir,
    }


# ------------------------------------------------------------------
# rl.monitor_training
# ------------------------------------------------------------------

def rl_monitor_training(*, training_id: str) -> dict[str, Any]:
    """Read training progress from progress.json."""
    info = _active_training.get(training_id)
    if info is None:
        return _error_result("UNKNOWN_TRAINING", f"Unknown training_id: {training_id}")

    output_dir = Path(info["output_dir"])
    progress_file = output_dir / "progress.json"

    result: dict[str, Any] = {
        "ok": True,
        "training_id": training_id,
        "pid": info["pid"],
        "elapsed_s": round(time.time() - info["started_at"], 1),
    }

    # Check if process is still running
    proc = info["process"]
    if proc.poll() is not None:
        result["process_status"] = "exited"
        result["return_code"] = proc.returncode
    else:
        result["process_status"] = "running"

    # Read progress file
    if progress_file.is_file():
        try:
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
            result["progress"] = progress
        except Exception:
            pass

    return result


# ------------------------------------------------------------------
# rl.stop_training
# ------------------------------------------------------------------

def rl_stop_training(*, training_id: str) -> dict[str, Any]:
    """SIGTERM the training subprocess."""
    info = _active_training.pop(training_id, None)
    if info is None:
        return {"ok": True, "stopped": True, "already_stopped": True}

    proc = info["process"]
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass

    return {
        "ok": True,
        "stopped": True,
        "return_code": proc.returncode,
        "training_id": training_id,
    }


# ------------------------------------------------------------------
# rl.deploy_policy
# ------------------------------------------------------------------

def rl_deploy_policy(
    *,
    training_id: str | None = None,
    checkpoint_dir: str | None = None,
    output_dir: str | None = None,
    alpha: float = 0.3,
) -> dict[str, Any]:
    """JIT export best checkpoint → return policy_path."""
    # Resolve checkpoint directory
    if checkpoint_dir is not None:
        ckpt_dir = Path(checkpoint_dir)
    elif training_id is not None:
        info = _active_training.get(training_id)
        if info is not None:
            ckpt_dir = Path(info["output_dir"])
        else:
            # Try standard location
            ckpt_dir = _PROJECT_ROOT / "training_runs" / training_id
    else:
        return _error_result(
            "INVALID_INPUT",
            "Provide either training_id or checkpoint_dir",
        )

    if not ckpt_dir.is_dir():
        return _error_result("DIR_NOT_FOUND", f"Directory not found: {ckpt_dir}")

    # Resolve output directory
    if output_dir is None:
        out = ckpt_dir / "deployed"
    else:
        out = Path(output_dir)

    # ── Resolution order ────────────────────────────────────────────
    # 1. Check if isaaclab_train.py already exported artifacts
    existing_policy = out / "policy.pt"
    existing_config = out / "deployment_config.json"
    if existing_policy.is_file() and existing_config.is_file():
        try:
            cfg = json.loads(existing_config.read_text(encoding="utf-8"))
            joint_names = cfg.get("joint_names", [])
            if joint_names:
                log.info("Using existing deployed artifacts in %s", out)
                return {
                    "ok": True,
                    "policy_path": str(existing_policy),
                    "config_path": str(existing_config),
                    "joint_names": joint_names,
                    "action_scale_per_joint": cfg.get("action_scale_per_joint"),
                    "alpha": cfg.get("alpha", alpha),
                    "reused_existing": True,
                }
        except Exception:
            pass  # Fall through to re-export

    # 2. Read joint_names from training_config.json (written by isaaclab_train.py)
    joint_names: list[str] = []
    action_scale: float = 0.3
    training_config_file = ckpt_dir / "training_config.json"
    if training_config_file.is_file():
        try:
            tc = json.loads(training_config_file.read_text(encoding="utf-8"))
            joint_names = tc.get("joint_names", [])
            # Use the average of per-joint scales for scalar fallback
            per_joint = tc.get("action_scale_per_joint", [])
            if per_joint:
                action_scale = sum(per_joint) / len(per_joint)
        except Exception:
            pass

    # 3. Fall back to env_config_path import
    if not joint_names and training_config_file.is_file():
        try:
            tc = json.loads(training_config_file.read_text(encoding="utf-8"))
            env_config_path = tc.get("env_config_path", "")
            if env_config_path and os.path.isfile(env_config_path):
                from rl_training.residual_env import build_env_config_from_file
                env_cfg = build_env_config_from_file(env_config_path)
                joint_names = env_cfg.joint_names or []
        except Exception:
            pass

    # 4. Error if joint_names still unresolved
    if not joint_names:
        return _error_result(
            "JOINT_NAMES_NOT_FOUND",
            "Cannot resolve joint_names from training_config.json or env_config_path. "
            "Ensure training was completed with isaaclab_train.py or provide a valid "
            "env_config_path in training_config.json.",
        )

    try:
        from rl_training.export_policy import export_policy
        result = export_policy(
            ckpt_dir, out,
            joint_names=joint_names,
            action_scale=action_scale,
            alpha=alpha,
        )
        return {"ok": True, **result}
    except FileNotFoundError as exc:
        return _error_result("CHECKPOINT_NOT_FOUND", str(exc))
    except Exception as exc:
        return _error_result("EXPORT_FAILED", f"Policy export failed: {exc}")


# ------------------------------------------------------------------
# rl.evaluate_policy
# ------------------------------------------------------------------

def rl_evaluate_policy(
    *,
    policy_path: str,
    urdf_path: str | None = None,
    num_episodes: int = 10,
) -> dict[str, Any]:
    """Run eval episodes → tracking accuracy metrics.

    Phase 1: basic validation that the policy loads and produces
    valid outputs.  Full Isaac Lab evaluation is Phase 2.
    """
    if not os.path.isfile(policy_path):
        return _error_result("POLICY_NOT_FOUND", f"Policy not found: {policy_path}")

    result: dict[str, Any] = {
        "ok": True,
        "policy_path": policy_path,
        "num_episodes": num_episodes,
    }

    # Validate policy loads
    try:
        import torch  # type: ignore[import-not-found]
        policy = torch.jit.load(policy_path, map_location="cpu")
        policy.eval()
        result["policy_loaded"] = True

        # Get output shape from a dummy forward pass
        # Try to infer input dim from deployment config
        config_file = Path(policy_path).parent / "deployment_config.json"
        obs_dim = 30  # default
        if config_file.is_file():
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            obs_dim = cfg.get("obs_dim", 30)

        dummy_obs = torch.zeros(1, obs_dim)
        with torch.no_grad():
            output = policy(dummy_obs)
        result["output_shape"] = list(output.shape)
        result["action_dim"] = output.shape[1] if len(output.shape) > 1 else output.shape[0]

    except ImportError:
        result["policy_loaded"] = False
        result["warning"] = "PyTorch not available — cannot validate policy"
    except Exception as exc:
        result["policy_loaded"] = False
        result["error_detail"] = str(exc)

    return result
