"""MCP tool implementations for the RL training pipeline.

Tools follow the same dispatch pattern as ``tools_motion.py`` and
``tools_study.py``.  Phase 1 uses subprocess management directly
(like ``study_runner.py``).

``rl_training`` owns URDF analysis, env-config generation, training and
policy export — core only orchestrates.  It runs on Isaac Sim's interpreter,
which core's venv cannot import from, so every call goes out as a subprocess
returning JSON: the CLI parity path (``docs/simulation-and-rl.md``).  Core
imports nothing from the RL package.
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

log = logging.getLogger("solidmind.tools_rl")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Active training subprocesses: training_id → process info
_active_training: dict[str, dict[str, Any]] = {}


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _rl_python() -> str:
    """Interpreter the RL pipeline runs on — Isaac Sim's, not core's.

    Isaac Lab lives in Isaac's bundled Python; core's venv cannot import it.
    ``ISAAC_PYTHON`` wins, then the conventional sibling source build, then
    core's interpreter as a last resort (enough for the URDF-only commands).
    """
    candidate = os.environ.get("ISAAC_PYTHON", "")
    if candidate and os.path.isfile(candidate):
        return candidate
    sibling = (
        _PROJECT_ROOT.parent / "isaacsim" / "_build" / "linux-x86_64" / "release" / "python.sh"
    )
    if sibling.is_file():
        return str(sibling)
    return sys.executable


def _run_rl_cli(command: list[str], *, timeout_s: float = 600.0) -> dict[str, Any]:
    """Run one ``rl_training.cli`` command and parse its JSON result.

    This is the CLI parity path: core talks to the RL pipeline the way it
    talks to an engine — a subprocess on its own interpreter, structured
    output — rather than importing it (``docs/simulation-and-rl.md``).
    """
    argv = [_rl_python(), "-m", "rl_training.cli", *command]
    log.info("rl cli: %s", " ".join(argv))
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        return _error_result(
            "RL_PIPELINE_UNAVAILABLE",
            f"Cannot run the RL pipeline: {exc}. Install solidmind-rl and set ISAAC_PYTHON.",
        )
    except subprocess.TimeoutExpired:
        return _error_result("RL_TIMEOUT", f"RL command timed out after {timeout_s}s")

    stdout = (proc.stdout or "").strip()
    if not stdout:
        detail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        return _error_result(
            "RL_PIPELINE_UNAVAILABLE",
            f"RL command produced no result (rc={proc.returncode}): {detail[0]}",
        )
    try:
        # The pipeline may log before its result; the JSON is the last line.
        result = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError as exc:
        return _error_result("RL_PROTOCOL_ERROR", f"Unparseable RL result: {exc}")
    if not isinstance(result, dict):
        return _error_result("RL_PROTOCOL_ERROR", "RL result was not an object")
    return result


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

    command = ["configure", "--urdf", urdf_path, "--num-envs", str(num_envs)]
    if output_path is None:
        output_dir = _PROJECT_ROOT / "training_runs"
        output_dir.mkdir(parents=True, exist_ok=True)
        # The pipeline names the file after the robot it found in the URDF.
        command += ["--output", str(output_dir / "env_config.py")]
    else:
        command += ["--output", output_path]

    return _run_rl_cli(command, timeout_s=300.0)


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
        _rl_python(),
        "-m",
        "rl_training.train",
        "--env-config",
        env_config,
        "--output-dir",
        output_dir,
    ]
    if max_iterations is not None:
        cmd.extend(["--max-iterations", str(max_iterations)])
    if num_envs is not None:
        cmd.extend(["--num-envs", str(num_envs)])

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
    # 1. Reuse what the trainer already exported, if it is complete.
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
        except (OSError, json.JSONDecodeError):
            pass  # fall through to a real export

    # 2. Otherwise export through the pipeline's own CLI, on its own
    #    interpreter — torch and the checkpoint format live there, not here.
    return _run_rl_cli(
        [
            "export",
            "--checkpoint-dir",
            str(ckpt_dir),
            "--output-dir",
            str(out),
            "--alpha",
            str(alpha),
        ],
        timeout_s=600.0,
    )


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
