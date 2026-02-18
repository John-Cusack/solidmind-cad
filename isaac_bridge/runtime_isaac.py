"""Isaac bridge runtime.

The runtime exposes deterministic command handlers for the bridge protocol.
When Omniverse Isaac APIs are available, a minimal physics stepping path is used.
Otherwise, a deterministic analytical fallback is used for local/CI execution.
"""
from __future__ import annotations

import base64
import logging
import math
import os
import queue
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import importlib

from isaac_bridge.models import SUPPORTED_JOINT_TYPES, SimulationSession, URDFImportConfig

logger = logging.getLogger("solidmind.isaac_runtime")

# USD prim type names that represent joints.  Checked via GetTypeName() which
# is reliable across Isaac Sim versions — unlike HasAPI() which only works for
# applied API schemas, not typed schemas.
_JOINT_TYPE_NAMES: frozenset[str] = frozenset({
    "PhysicsRevoluteJoint",
    "PhysicsPrismaticJoint",
    "PhysicsFixedJoint",
    "PhysicsSphericalJoint",
    "PhysicsDistanceJoint",
    "PhysicsJoint",
})


class IsaacRuntimeError(Exception):
    """Runtime-level error with structured code/message/details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(slots=True)
class _EngineResult:
    mode: str
    steps: int
    warning: str | None = None
    joint_samples: list[dict[str, Any]] | None = None
    prim_path: str | None = None
    joint_count: int = 0
    link_count: int = 0


class _MainThreadDispatcher:
    """Dispatch callables to the main thread and wait for results.

    In non-headless mode, Isaac Sim requires all USD/physics operations on
    the main thread.  Worker threads (TCP handlers) submit work here; the
    main-thread Kit pump calls ``process_pending()`` each tick.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[
            tuple[Any, tuple, dict, threading.Event, list]
        ] = queue.Queue()
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Submit *fn* for main-thread execution; block until done."""
        if not self._enabled:
            # Dispatcher not active (headless) — run inline.
            return fn(*args, **kwargs)
        event = threading.Event()
        result_box: list[Any] = [None, None]  # [result, exception]
        self._queue.put((fn, args, kwargs, event, result_box))
        logger.debug("[dispatch] submitted %s, waiting...", getattr(fn, "__name__", fn))
        event.wait()
        if result_box[1] is not None:
            raise result_box[1]
        return result_box[0]

    def process_pending(self) -> int:
        """Execute all queued callables on the current (main) thread.

        Returns the number of items processed.
        """
        count = 0
        while True:
            try:
                fn, args, kwargs, event, result_box = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                result_box[0] = fn(*args, **kwargs)
            except Exception as exc:
                result_box[1] = exc
            finally:
                event.set()
                count += 1
        return count


# Module-level dispatcher — shared between engine, runtime, and bridge_server.
main_thread_dispatcher = _MainThreadDispatcher()


class _IsaacWorldEngine:
    """Optional minimal integration with Isaac Sim APIs."""

    def __init__(self, *, headless: bool = True, sim_app: Any = None) -> None:
        self._headless = headless
        self._available = False
        self._world_type: Any = None
        self._sim_app: Any = sim_app
        self._scene_ready = False
        self._imported_prims: list[str] = []
        self._world: Any = None
        self._detect()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def sim_app(self) -> Any:
        """The SimulationApp instance, if available."""
        return self._sim_app

    def _detect(self) -> None:
        # SimulationApp must be created before any omni.* imports work.
        # If a sim_app was passed in (hot-reload path), skip creation.
        if self._sim_app is not None:
            logger.info("[engine] Reusing existing SimulationApp (hot-reload)")
        else:
            logger.info("[engine] Detecting Isaac Sim (headless=%s)...", self._headless)
            try:
                from isaacsim import SimulationApp  # type: ignore[import-not-found]

                logger.info("[engine] Creating SimulationApp...")
                self._sim_app = SimulationApp({"headless": self._headless})
                logger.info("[engine] SimulationApp created OK")
            except Exception as exc:
                logger.warning("[engine] SimulationApp creation failed: %s", exc)
                self._available = False
                return

        try:
            from omni.isaac.core import World  # type: ignore[import-not-found]
        except Exception as exc:
            logger.warning("[engine] World import failed: %s", exc)
            self._available = False
            return
        self._world_type = World
        self._available = True
        logger.info("[engine] Isaac Sim detected and available")

    @property
    def world(self) -> Any:
        """The active World instance.  Raises if ``setup_scene`` hasn't run."""
        if self._world is None:
            raise IsaacRuntimeError(
                "ENGINE_NOT_READY",
                "No active World — call setup_scene() first",
            )
        return self._world

    def setup_scene(self, *, physics_dt: float | None = None) -> None:
        """Create World, physics scene, ground plane, and lighting. Idempotent."""
        if self._scene_ready:
            logger.debug("[engine] setup_scene: already ready, skipping")
            return
        logger.info("[engine] setup_scene: creating World + physics scene, ground, lighting...")

        # Create the World — single owner for the entire engine lifecycle.
        world_kwargs: dict[str, Any] = {"stage_units_in_meters": 1.0}
        if physics_dt is not None:
            world_kwargs["physics_dt"] = physics_dt
            world_kwargs["rendering_dt"] = physics_dt
        self._world = self._world_type(**world_kwargs)

        try:
            from pxr import UsdPhysics, Gf, UsdLux  # type: ignore[import-not-found]
            import omni.usd  # type: ignore[import-not-found]

            stage = omni.usd.get_context().get_stage()

            # Physics scene
            scene_path = "/World/PhysicsScene"
            if not stage.GetPrimAtPath(scene_path).IsValid():
                scene_prim = stage.DefinePrim(scene_path, "PhysicsScene")
                UsdPhysics.Scene(scene_prim).CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
                UsdPhysics.Scene(scene_prim).CreateGravityMagnitudeAttr(9.81)

            # Ground plane
            ground_path = "/World/GroundPlane"
            if not stage.GetPrimAtPath(ground_path).IsValid():
                from omni.isaac.core.objects import GroundPlane  # type: ignore[import-not-found]
                GroundPlane(prim_path=ground_path)

            # Distant light
            light_path = "/World/DistantLight"
            if not stage.GetPrimAtPath(light_path).IsValid():
                light_prim = stage.DefinePrim(light_path, "DistantLight")
                UsdLux.DistantLight(light_prim).CreateIntensityAttr(3000)

            self._scene_ready = True
            logger.info("[engine] setup_scene: done")
        except Exception as exc:
            logger.warning("[engine] setup_scene failed (non-fatal): %s", exc)

    def import_urdf(
        self,
        urdf_path: str,
        config: URDFImportConfig,
    ) -> tuple[str, int, int]:
        """Import a URDF file. Returns (prim_path, joint_count, link_count)."""
        logger.info("[engine] import_urdf: %s", urdf_path)
        t0 = time.monotonic()
        import omni.kit.commands  # type: ignore[import-not-found]
        import omni.usd  # type: ignore[import-not-found]

        # Create import config
        logger.info("[engine] import_urdf: creating import config...")
        _ok, import_config = omni.kit.commands.execute(
            "URDFCreateImportConfig",
        )
        import_config.merge_fixed_joints = config.merge_fixed_joints
        import_config.convex_decomp = config.convex_decomp
        import_config.import_inertia_tensor = config.import_inertia_tensor
        import_config.fix_base = config.fix_base
        import_config.distance_scale = config.distance_scale
        # Isaac Sim 4.x uses typed enums for drive type instead of plain ints.
        # Discover the enum from the config object's module.
        _drive_type_set = False
        try:
            import sys
            _urdf_mod = sys.modules.get("isaacsim.asset.importer.urdf._urdf")
            if _urdf_mod is None:
                import isaacsim.asset.importer.urdf._urdf as _urdf_mod  # type: ignore[import-not-found]
            UrdfJointTargetType = getattr(_urdf_mod, "UrdfJointTargetType", None)
            if UrdfJointTargetType is not None:
                import_config.default_drive_type = (
                    UrdfJointTargetType.JOINT_DRIVE_VELOCITY
                    if config.default_drive_type == "velocity"
                    else UrdfJointTargetType.JOINT_DRIVE_POSITION
                )
                _drive_type_set = True
                logger.info("[engine] import_urdf: using UrdfJointTargetType enum for drive type")
            else:
                logger.warning(
                    "[engine] import_urdf: UrdfJointTargetType not found in _urdf module. "
                    "Available attrs: %s", [a for a in dir(_urdf_mod) if "rdf" in a.lower() or "oint" in a.lower() or "rive" in a.lower()],
                )
        except Exception as exc:
            logger.warning("[engine] import_urdf: failed to load UrdfJointTargetType: %r", exc)
        if not _drive_type_set:
            import_config.default_drive_type = (
                1 if config.default_drive_type == "velocity" else 0
            )
        # Stiffness/damping attribute names vary across Isaac Sim versions.
        _cfg_attrs = dir(import_config)
        for attr, value in [
            ("default_drive_strength", config.default_drive_stiffness),
            ("default_drive_stiffness", config.default_drive_stiffness),
            ("default_drive_damping", config.default_drive_damping),
        ]:
            if attr in _cfg_attrs:
                try:
                    setattr(import_config, attr, value)
                except Exception as exc:
                    logger.warning("[engine] import_urdf: failed to set %s: %r", attr, exc)
        logger.info(
            "[engine] import_urdf: ImportConfig drive attrs available: %s",
            [a for a in _cfg_attrs if "drive" in a.lower() or "damp" in a.lower() or "stiff" in a.lower()],
        )

        logger.info("[engine] import_urdf: calling URDFParseAndImportFile...")
        _ok2, prim_path = omni.kit.commands.execute(
            "URDFParseAndImportFile",
            urdf_path=urdf_path,
            import_config=import_config,
        )
        logger.info("[engine] import_urdf: URDFParseAndImportFile returned prim_path=%s", prim_path)
        if not prim_path:
            raise IsaacRuntimeError(
                "URDF_IMPORT_FAILED",
                f"URDFParseAndImportFile returned empty prim_path for {urdf_path}",
            )

        self._imported_prims.append(prim_path)

        # Count joints and links, log all unique type names for debugging
        stage = omni.usd.get_context().get_stage()
        joint_count = 0
        link_count = 0
        type_name_counts: dict[str, int] = {}
        root_prim = stage.GetPrimAtPath(prim_path)
        if root_prim.IsValid():
            from pxr import Usd  # type: ignore[import-not-found]
            for prim in Usd.PrimRange(root_prim):
                type_name = prim.GetTypeName()
                if type_name:
                    type_name_counts[type_name] = type_name_counts.get(type_name, 0) + 1
                if type_name in _JOINT_TYPE_NAMES:
                    joint_count += 1
                if type_name in ("Xform", "Mesh"):
                    link_count += 1

        logger.info(
            "[engine] import_urdf: prim type census: %s",
            dict(sorted(type_name_counts.items())),
        )
        logger.info(
            "[engine] import_urdf: done in %.3fs — prim=%s joints=%d links=%d",
            time.monotonic() - t0, prim_path, joint_count, link_count,
        )
        return prim_path, joint_count, link_count

    def create_articulation(self, prim_path: str) -> Any:
        """Add an Articulation to the World scene and initialize physics.

        Isaac Sim 4.x requires adding the articulation to ``world.scene``
        and calling ``world.reset()`` so that the physics backend properly
        creates the articulation view.  Calling ``art.initialize()``
        directly leaves the internal physics handle as None.

        Requires ``setup_scene()`` to have been called first.
        """
        logger.info("[engine] create_articulation: %s", prim_path)
        t0 = time.monotonic()
        from omni.isaac.core.articulations import Articulation  # type: ignore[import-not-found]

        # Derive a scene-unique name from the prim path (e.g. "/World/robot" → "robot").
        art_name = prim_path.rsplit("/", 1)[-1] or "robot"

        world = self.world  # raises if setup_scene() wasn't called
        logger.info("[engine] create_articulation: adding to scene as %r...", art_name)
        world.scene.add(Articulation(prim_path=prim_path, name=art_name))
        logger.info("[engine] create_articulation: calling world.reset()...")
        world.reset()
        art = world.scene.get_object(art_name)

        # world.reset() initializes all physics handles.  Isaac Sim 4.x
        # returns a SingleArticulation which doesn't expose
        # is_physics_handle_valid(), so we just verify we got an object back.
        if art is None:
            raise IsaacRuntimeError(
                "ARTICULATION_INIT_FAILED",
                f"world.scene.get_object({art_name!r}) returned None for {prim_path}",
            )

        logger.info("[engine] create_articulation: done in %.3fs", time.monotonic() - t0)
        return art

    def apply_drives(
        self,
        prim_path: str,
        mechanism: dict[str, Any],
    ) -> list[str]:
        """Apply drive targets from mechanism to joint prims. Returns warnings."""
        warnings: list[str] = []
        drives = mechanism.get("drives", [])
        joints = mechanism.get("joints", [])
        if not isinstance(drives, list) or not isinstance(joints, list):
            return warnings

        import omni.usd  # type: ignore[import-not-found]
        from pxr import Usd, UsdPhysics, Gf  # type: ignore[import-not-found]

        stage = omni.usd.get_context().get_stage()
        joint_by_id: dict[str, dict[str, Any]] = {}
        for j in joints:
            if isinstance(j, dict) and isinstance(j.get("id"), str):
                joint_by_id[j["id"]] = j

        for drive in drives:
            if not isinstance(drive, dict):
                continue
            speed_rpm = drive.get("speed_rpm")
            if not isinstance(speed_rpm, (int, float)):
                continue
            joint_id = drive.get("joint_id")
            if not isinstance(joint_id, str):
                continue

            # Convert RPM to deg/s for revolute joints
            speed_deg_s = float(speed_rpm) * 6.0  # RPM * 360/60

            # Find the joint prim by name under the robot prim
            root_prim = stage.GetPrimAtPath(prim_path)
            if not root_prim.IsValid():
                warnings.append(f"Root prim {prim_path} not valid")
                continue

            found = False
            for prim in Usd.PrimRange(root_prim):
                if joint_id in prim.GetName():
                    drive_api = UsdPhysics.DriveAPI.Get(prim, "angular")
                    if drive_api:
                        drive_api.GetTargetVelocityAttr().Set(speed_deg_s)
                        drive_api.GetDampingAttr().Set(1e4)
                        drive_api.GetStiffnessAttr().Set(0.0)
                        found = True
                        break
            if not found:
                warnings.append(f"Drive joint '{joint_id}' not found in URDF prim tree")

        return warnings

    def start_simulation(
        self,
        urdf_path: str,
        config: URDFImportConfig,
        mechanism: dict[str, Any] | None = None,
    ) -> tuple[str, int, int, Any, list[str]]:
        """Setup scene, import URDF, create articulation, apply drives.

        All Isaac API calls are dispatched to the main thread when
        running in non-headless mode.

        Returns (prim_path, joint_count, link_count, articulation, warnings).
        """
        logger.info("[engine] start_simulation: beginning setup for %s", urdf_path)
        t0 = time.monotonic()

        def _do_setup() -> tuple[str, int, int, Any, list[str]]:
            """Runs on the main thread (via dispatcher)."""
            self.setup_scene()
            pp, jc, lc = self.import_urdf(urdf_path, config)
            art = self.create_articulation(pp)
            warns: list[str] = []
            if mechanism:
                logger.info("[engine] start_simulation: applying drives...")
                warns = self.apply_drives(pp, mechanism)
                if warns:
                    logger.warning("[engine] start_simulation: drive warnings: %s", warns)
            return pp, jc, lc, art, warns

        result = main_thread_dispatcher.submit(_do_setup)
        prim_path, joint_count, link_count, articulation, warnings = result

        logger.info(
            "[engine] start_simulation: complete in %.3fs — prim=%s joints=%d links=%d",
            time.monotonic() - t0, prim_path, joint_count, link_count,
        )
        return prim_path, joint_count, link_count, articulation, warnings

    def step_and_sample(
        self,
        n_steps: int,
        articulation: Any,
        sample_every: int,
    ) -> list[dict[str, Any]]:
        """Step world and sample joint states at intervals.

        Dispatched to the main thread in non-headless mode.
        """
        logger.info("[engine] step_and_sample: %d steps, sample_every=%d", n_steps, sample_every)
        t0 = time.monotonic()

        def _do_step() -> list[dict[str, Any]]:
            world = self.world  # must be the same World that owns the articulation
            results: list[dict[str, Any]] = []
            for step_i in range(n_steps):
                world.step(render=False)
                if step_i > 0 and step_i % 500 == 0:
                    logger.info(
                        "[engine] step_and_sample: step %d/%d (%.1fs elapsed)",
                        step_i, n_steps, time.monotonic() - t0,
                    )
                if step_i % sample_every == 0 or step_i == n_steps - 1:
                    try:
                        positions = articulation.get_joint_positions()
                        velocities = articulation.get_joint_velocities()
                        sample: dict[str, Any] = {"step": step_i}
                        if positions is not None:
                            sample["joint_positions"] = [float(p) for p in positions]
                        if velocities is not None:
                            sample["joint_velocities"] = [float(v) for v in velocities]
                        results.append(sample)
                    except Exception:
                        pass  # Non-fatal — skip this sample
            return results

        samples = main_thread_dispatcher.submit(_do_step)

        logger.info(
            "[engine] step_and_sample: done in %.3fs — %d samples collected",
            time.monotonic() - t0, len(samples),
        )
        return samples

    def cleanup(self) -> None:
        """Stop timeline and remove imported prims.

        Dispatched to the main thread in non-headless mode.
        """
        def _do_cleanup() -> None:
            try:
                import omni.timeline  # type: ignore[import-not-found]
                timeline = omni.timeline.get_timeline_interface()
                timeline.stop()
            except Exception:
                pass

            # Tear down the World so next run starts fresh via setup_scene().
            self._world = None
            self._scene_ready = False

            try:
                import omni.usd  # type: ignore[import-not-found]
                stage = omni.usd.get_context().get_stage()
                for prim_path in self._imported_prims:
                    prim = stage.GetPrimAtPath(prim_path)
                    if prim.IsValid():
                        stage.RemovePrim(prim_path)
                self._imported_prims.clear()
            except Exception:
                pass

        main_thread_dispatcher.submit(_do_cleanup)

    def run(
        self,
        *,
        duration_s: float,
        dt_s: float,
        urdf_path: str | None = None,
        import_config: URDFImportConfig | None = None,
        mechanism: dict[str, Any] | None = None,
    ) -> _EngineResult:
        n_steps = max(1, int(math.ceil(duration_s / dt_s)))

        if not self._available:
            return _EngineResult(
                mode="reference",
                steps=n_steps,
                warning="Isaac Sim not available, using reference mode." if urdf_path else None,
            )

        if urdf_path is None:
            # Original path: empty world stepping — dispatch to main thread.
            def _empty_world_step() -> _EngineResult:
                self.setup_scene(physics_dt=dt_s)
                for _ in range(n_steps):
                    self.world.step(render=False)
                self.cleanup()
                return _EngineResult(mode="isaac", steps=n_steps)

            try:
                return main_thread_dispatcher.submit(_empty_world_step)
            except Exception as exc:
                return _EngineResult(
                    mode="reference",
                    steps=n_steps,
                    warning=f"Isaac runtime stepping failed, fell back to reference mode: {exc}",
                )

        # URDF-aware path — uses start_simulation / step_and_sample / cleanup
        # which are already dispatched individually.
        try:
            cfg = import_config or URDFImportConfig()
            prim_path, joint_count, link_count, articulation, drive_warnings = (
                self.start_simulation(urdf_path, cfg, mechanism)
            )

            sample_every = max(1, n_steps // 100)  # ~100 samples max
            joint_samples = self.step_and_sample(n_steps, articulation, sample_every)

            self.cleanup()

            result = _EngineResult(
                mode="isaac_urdf",
                steps=n_steps,
                joint_samples=joint_samples,
                prim_path=prim_path,
                joint_count=joint_count,
                link_count=link_count,
            )
            if drive_warnings:
                result.warning = "; ".join(drive_warnings)
            return result
        except IsaacRuntimeError:
            self.cleanup()
            raise
        except Exception as exc:
            self.cleanup()
            return _EngineResult(
                mode="reference",
                steps=n_steps,
                warning=f"URDF import/simulation failed, fell back to reference mode: {exc}",
            )


class IsaacRuntime:
    """Command runtime used by the bridge server."""

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._sessions: dict[str, SimulationSession] = {}
        self._batch_threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._engine = _IsaacWorldEngine(headless=headless)

    def ping(self) -> dict[str, Any]:
        return {
            "pong": True,
            "bridge_version": "1.0.0",
            "capabilities": {
                "commands": [
                    "ping",
                    "diagnose",
                    "reload",
                    "import_urdf",
                    "simulate",
                    "simulate_start",
                    "simulate_status",
                    "simulate_stop",
                    "teleop_start",
                    "teleop_command",
                    "teleop_state",
                    "teleop_stop",
                    "screenshot",
                ],
                "supported_joint_types": sorted(SUPPORTED_JOINT_TYPES),
                "headless_default": self._headless,
                "isaac_available": self._engine.available,
            },
        }

    def diagnose(self, *, prim_path: str = "/") -> dict[str, Any]:
        """Dump the USD prim tree under *prim_path*.

        Returns each prim's path, type name, and applied schemas, plus
        summary counts by type.  Works without an articulation — just
        reads the stage.
        """
        if not self._engine.available:
            raise IsaacRuntimeError(
                "ISAAC_NOT_AVAILABLE",
                "Isaac Sim is not available — cannot inspect stage.",
            )

        def _do_diagnose() -> dict[str, Any]:
            import omni.usd  # type: ignore[import-not-found]
            from pxr import Usd  # type: ignore[import-not-found]

            stage = omni.usd.get_context().get_stage()
            root = stage.GetPrimAtPath(prim_path)
            if not root.IsValid():
                return {
                    "prim_path": prim_path,
                    "error": f"Prim not found: {prim_path}",
                    "prims": [],
                    "type_counts": {},
                }

            prims: list[dict[str, Any]] = []
            type_counts: dict[str, int] = {}
            for prim in Usd.PrimRange(root):
                type_name = prim.GetTypeName() or ""
                schemas = [str(s) for s in prim.GetAppliedSchemas()]
                prims.append({
                    "path": str(prim.GetPath()),
                    "type": type_name,
                    "applied_schemas": schemas,
                })
                if type_name:
                    type_counts[type_name] = type_counts.get(type_name, 0) + 1

            return {
                "prim_path": prim_path,
                "prim_count": len(prims),
                "prims": prims,
                "type_counts": dict(sorted(type_counts.items())),
            }

        return main_thread_dispatcher.submit(_do_diagnose)

    def reload(self) -> dict[str, Any]:
        """Hot-reload runtime code without restarting SimulationApp.

        Tears down the current engine's World/prims, reloads the
        ``isaac_bridge.runtime_isaac`` module, and recreates the engine
        reusing the existing SimulationApp instance.

        Returns a status dict.  The caller (BridgeServer) must replace
        its ``_runtime`` reference with the freshly created instance
        returned via the ``new_runtime`` key.
        """
        logger.info("[runtime] reload: tearing down engine...")
        # Grab the SimulationApp before cleanup
        sim_app = self._engine.sim_app
        headless = self._headless

        # Cleanup existing World/prims
        try:
            self._engine.cleanup()
        except Exception as exc:
            logger.warning("[runtime] reload: cleanup error (non-fatal): %s", exc)

        # Reload the module to pick up code changes
        logger.info("[runtime] reload: reloading isaac_bridge.runtime_isaac...")
        import isaac_bridge.runtime_isaac as _self_mod
        importlib.reload(_self_mod)

        # Recreate engine with the existing SimulationApp
        logger.info("[runtime] reload: recreating engine with existing SimulationApp...")
        new_engine = _self_mod._IsaacWorldEngine(headless=headless, sim_app=sim_app)
        new_runtime = _self_mod.IsaacRuntime.__new__(_self_mod.IsaacRuntime)
        new_runtime._headless = headless
        new_runtime._sessions = {}
        new_runtime._batch_threads = {}
        new_runtime._lock = threading.RLock()
        new_runtime._engine = new_engine

        logger.info("[runtime] reload: done — new runtime ready")
        return {
            "reloaded": True,
            "isaac_available": new_engine.available,
            "new_runtime": new_runtime,  # BridgeServer swaps this in
        }

    def import_urdf(
        self,
        *,
        urdf_path: str,
        import_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Import a URDF file into the Isaac scene. Standalone command."""
        if not os.path.isfile(urdf_path):
            raise IsaacRuntimeError(
                "URDF_NOT_FOUND",
                f"URDF file not found: {urdf_path}",
            )
        if not self._engine.available:
            raise IsaacRuntimeError(
                "ISAAC_NOT_AVAILABLE",
                "Isaac Sim is not available — cannot import URDF.",
            )
        config = URDFImportConfig.from_dict(import_config)

        def _do_import() -> tuple[str, int, int]:
            self._engine.setup_scene()
            return self._engine.import_urdf(urdf_path, config)

        prim_path, joint_count, link_count = main_thread_dispatcher.submit(_do_import)
        return {
            "prim_path": prim_path,
            "joint_count": joint_count,
            "link_count": link_count,
        }

    # ------------------------------------------------------------------
    # Simulation session lifecycle
    # ------------------------------------------------------------------

    def simulate_start(
        self,
        *,
        mechanism: dict[str, Any],
        duration_s: float,
        dt_s: float,
        output_interval: float,
        profile: dict[str, Any] | None = None,
        urdf_path: str | None = None,
        import_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Non-blocking. Setup scene + begin physics. Returns session_id."""
        logger.info(
            "[runtime] simulate_start: duration=%.3f dt=%.4f urdf=%s headless=%s",
            duration_s, dt_s, urdf_path, self._headless,
        )
        t0 = time.monotonic()
        mech = _validate_mechanism(mechanism)
        _validate_sim_args(
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
        )
        unsupported = _unsupported_joints(mech)
        if unsupported:
            raise IsaacRuntimeError(
                "UNSUPPORTED_JOINT_TYPE",
                "Mechanism contains unsupported joint types for Isaac bridge v1",
                details={
                    "unsupported_joints": unsupported,
                    "supported_joint_types": sorted(SUPPORTED_JOINT_TYPES),
                },
            )

        if urdf_path is not None and not os.path.isfile(urdf_path):
            raise IsaacRuntimeError(
                "URDF_NOT_FOUND",
                f"URDF file not found: {urdf_path}",
            )

        n_steps = max(1, int(math.ceil(duration_s / dt_s)))
        speeds = _steady_state_speeds(mech)
        session_id = f"sim_{uuid.uuid4().hex[:12]}"
        now = time.time()

        prim_path: str | None = None
        articulation: Any = None
        joint_count = 0
        link_count = 0
        warnings: list[str] = []
        interactive = False

        if urdf_path and self._engine.available:
            logger.info("[runtime] simulate_start: URDF path provided and engine available — setting up...")
            try:
                cfg = URDFImportConfig.from_dict(import_config)
                prim_path, joint_count, link_count, articulation, drive_warns = (
                    self._engine.start_simulation(urdf_path, cfg, mech)
                )
                warnings.extend(drive_warns)
                logger.info("[runtime] simulate_start: engine setup complete")
            except IsaacRuntimeError:
                raise
            except Exception as exc:
                logger.error("[runtime] simulate_start: engine setup failed: %s", exc, exc_info=True)
                warnings.append(f"URDF import/setup failed: {exc}")

            if not self._headless and articulation is not None:
                interactive = True
                logger.info("[runtime] simulate_start: non-headless + articulation → interactive mode")
        elif urdf_path:
            logger.info("[runtime] simulate_start: URDF provided but engine not available — reference mode")
        else:
            logger.info("[runtime] simulate_start: no URDF — reference/analytical mode")

        session = SimulationSession(
            session_id=session_id,
            session_type="simulate",
            mechanism=mech,
            profile=dict(profile or {}),
            started_at_s=now,
            prim_path=prim_path,
            articulation=articulation,
            target_steps=0 if interactive else n_steps,
            status="running",
            warning="; ".join(warnings) if warnings else None,
        )

        # Pre-populate analytical time series as samples for reference mode
        if articulation is None or not self._engine.available:
            part_ids = [
                p["id"]
                for p in mech.get("parts", [])
                if isinstance(p, dict) and isinstance(p.get("id"), str)
            ]
            sample_times = _sample_times(duration_s=duration_s, output_interval=output_interval)
            for t in sample_times:
                session.samples.append({
                    "t": t,
                    "parts": {pid: {"omega_rpm": float(speeds.get(pid, 0.0))} for pid in part_ids},
                })
            session.completed_steps = n_steps
            session.status = "complete"

        with self._lock:
            self._sessions[session_id] = session

        # In headless mode with a live articulation, spawn batch stepping thread
        if self._headless and articulation is not None and session.status == "running":
            sample_every = max(1, n_steps // 100)
            logger.info("[runtime] simulate_start: spawning batch worker thread (%d steps)", n_steps)
            thread = threading.Thread(
                target=self._batch_step_worker,
                args=(session, n_steps, sample_every, dt_s, speeds),
                daemon=True,
            )
            with self._lock:
                self._batch_threads[session_id] = thread
            thread.start()

        if interactive:
            session.status = "complete"
            session.completed_steps = 0

        logger.info(
            "[runtime] simulate_start: returning session=%s status=%s interactive=%s (%.3fs)",
            session_id, session.status, interactive, time.monotonic() - t0,
        )
        result: dict[str, Any] = {
            "session_id": session_id,
            "status": session.status,
            "target_steps": session.target_steps,
            "steady_state_speeds": {
                pid: float(speeds.get(pid, 0.0))
                for pid in [
                    p["id"]
                    for p in mech.get("parts", [])
                    if isinstance(p, dict) and isinstance(p.get("id"), str)
                ]
            },
            "profile_used": dict(profile or {}),
        }
        if interactive:
            result["interactive"] = True
        if prim_path:
            result["prim_path"] = prim_path
            result["joint_count"] = joint_count
            result["link_count"] = link_count
        if warnings:
            result["warnings"] = warnings
        return result

    def _batch_step_worker(
        self,
        session: SimulationSession,
        n_steps: int,
        sample_every: int,
        dt_s: float,
        speeds: dict[str, float],
    ) -> None:
        """Background thread: step physics, collect samples, mark complete."""
        logger.info("[runtime] batch_worker: starting %d steps for session %s", n_steps, session.session_id)
        t0 = time.monotonic()
        try:
            if session.articulation is not None:
                joint_samples = self._engine.step_and_sample(
                    n_steps, session.articulation, sample_every,
                )
                part_ids = [
                    p["id"]
                    for p in session.mechanism.get("parts", [])
                    if isinstance(p, dict) and isinstance(p.get("id"), str)
                ]
                for sample in joint_samples:
                    step_i = sample.get("step", 0)
                    t = round(step_i * dt_s, 9)
                    entry: dict[str, Any] = {"t": t}
                    if "joint_positions" in sample:
                        entry["joint_positions"] = sample["joint_positions"]
                    if "joint_velocities" in sample:
                        entry["joint_velocities"] = sample["joint_velocities"]
                    entry["parts"] = {
                        pid: {"omega_rpm": float(speeds.get(pid, 0.0))} for pid in part_ids
                    }
                    session.samples.append(entry)
                session.completed_steps = n_steps
            else:
                # No articulation — stepping without URDF
                for step_i in range(n_steps):
                    if session.status != "running":
                        break
                    self._engine.world.step(render=False)
                    session.completed_steps = step_i + 1
                session.completed_steps = n_steps
        except Exception as exc:
            logger.error("[runtime] batch_worker: error: %s", exc, exc_info=True)
        finally:
            session.status = "complete"
            logger.info(
                "[runtime] batch_worker: done in %.3fs — session=%s steps=%d samples=%d",
                time.monotonic() - t0, session.session_id, session.completed_steps, len(session.samples),
            )
            with self._lock:
                self._batch_threads.pop(session.session_id, None)

    def simulate_status(self, *, session_id: str) -> dict[str, Any]:
        """Non-blocking. Returns progress, state, samples so far."""
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise IsaacRuntimeError(
                "ISAAC_UNKNOWN_SESSION",
                f"unknown simulation session {session_id}",
            )
        if session.session_type != "simulate":
            raise IsaacRuntimeError(
                "ISAAC_WRONG_SESSION_TYPE",
                f"session {session_id} is a {session.session_type} session, not simulate",
            )
        return {
            "status": session.status,
            "completed_steps": session.completed_steps,
            "target_steps": session.target_steps,
            "samples_count": len(session.samples),
        }

    def simulate_stop(self, *, session_id: str) -> dict[str, Any]:
        """Stop simulation, return final samples, cleanup."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            thread = self._batch_threads.pop(session_id, None)
        if session is None:
            return {"stopped": True, "already_stopped": True}

        # Signal the batch thread to stop and wait for it
        session.status = "complete"
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

        # Cleanup engine resources
        if session.prim_path:
            try:
                self._engine.cleanup()
            except Exception:
                pass

        result: dict[str, Any] = {
            "stopped": True,
            "completed_steps": session.completed_steps,
            "target_steps": session.target_steps,
            "samples": session.samples,
        }
        if session.warning:
            result["warnings"] = [session.warning]
        return result

    def simulate(
        self,
        *,
        mechanism: dict[str, Any],
        duration_s: float,
        dt_s: float,
        output_interval: float,
        profile: dict[str, Any] | None = None,
        urdf_path: str | None = None,
        import_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Synchronous wrapper: start -> poll -> stop -> return aggregated result."""
        start_result = self.simulate_start(
            mechanism=mechanism,
            duration_s=duration_s,
            dt_s=dt_s,
            output_interval=output_interval,
            profile=profile,
            urdf_path=urdf_path,
            import_config=import_config,
        )
        session_id = start_result["session_id"]

        if start_result.get("interactive"):
            # Non-headless: return immediately with session info
            return start_result

        # Headless batch: block until complete
        while True:
            status = self.simulate_status(session_id=session_id)
            if status["status"] == "complete":
                break
            time.sleep(0.01)

        stop_result = self.simulate_stop(session_id=session_id)

        # Build backward-compatible response
        samples = stop_result.get("samples", [])
        speeds = start_result.get("steady_state_speeds", {})
        part_ids = list(speeds.keys())

        # If samples already have 't' key, use them directly as time_series
        if samples and "t" in samples[0]:
            time_series = samples
        else:
            # Engine samples need time mapping
            time_series = samples

        result: dict[str, Any] = {
            "time_series": time_series,
            "summary": {
                "simulation_time_s": duration_s,
                "time_steps": stop_result.get("target_steps", 0),
                "output_samples": len(time_series),
                "steady_state_speeds": speeds,
                "engine_mode": "isaac_urdf" if start_result.get("prim_path") else "reference",
            },
            "profile_used": start_result.get("profile_used", {}),
        }
        if start_result.get("prim_path"):
            result["summary"]["prim_path"] = start_result["prim_path"]
            result["summary"]["joint_count"] = start_result.get("joint_count", 0)
            result["summary"]["link_count"] = start_result.get("link_count", 0)
        warnings = start_result.get("warnings") or stop_result.get("warnings")
        if warnings:
            result["warnings"] = warnings
        return result

    # ------------------------------------------------------------------
    # Teleop session lifecycle
    # ------------------------------------------------------------------

    def teleop_start(
        self,
        *,
        mechanism: dict[str, Any],
        profile: dict[str, Any] | None = None,
        urdf_path: str | None = None,
        import_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mech = _validate_mechanism(mechanism)
        unsupported = _unsupported_joints(mech)
        if unsupported:
            raise IsaacRuntimeError(
                "UNSUPPORTED_JOINT_TYPE",
                "Mechanism contains unsupported joint types for Isaac bridge v1",
                details={
                    "unsupported_joints": unsupported,
                    "supported_joint_types": sorted(SUPPORTED_JOINT_TYPES),
                },
            )

        # Validate URDF path if provided
        if urdf_path is not None and not os.path.isfile(urdf_path):
            raise IsaacRuntimeError(
                "URDF_NOT_FOUND",
                f"URDF file not found: {urdf_path}",
            )

        prim_path: str | None = None
        articulation: Any = None
        warnings: list[str] = []

        if urdf_path and self._engine.available:
            try:
                cfg = URDFImportConfig.from_dict(import_config)
                prim_path, _jc, _lc, articulation, drive_warns = (
                    self._engine.start_simulation(urdf_path, cfg, mech)
                )
                warnings.extend(drive_warns)
            except IsaacRuntimeError:
                raise
            except Exception as exc:
                warnings.append(f"URDF import for teleop failed: {exc}")

        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        now = time.time()
        session = SimulationSession(
            session_id=session_id,
            session_type="teleop",
            mechanism=mech,
            profile=dict(profile or {}),
            started_at_s=now,
            prim_path=prim_path,
            articulation=articulation,
        )
        with self._lock:
            self._sessions[session_id] = session

        result: dict[str, Any] = {
            "session_id": session_id,
            "status": "started",
            "keyboard_bindings": {
                "forward_back": "W/S",
                "turn": "A/D",
                "body_height": "Q/E",
            },
            "state": session.state.to_dict(),
            "profile_used": dict(profile or {}),
        }
        if prim_path:
            result["prim_path"] = prim_path
        if warnings:
            result["warnings"] = warnings
        return result

    def teleop_command(
        self,
        *,
        session_id: str,
        vx_mps: float,
        yaw_rate_rps: float,
        body_height_m: float,
    ) -> dict[str, Any]:
        _validate_finite("vx_mps", vx_mps)
        _validate_finite("yaw_rate_rps", yaw_rate_rps)
        _validate_finite("body_height_m", body_height_m)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise IsaacRuntimeError(
                    "ISAAC_UNKNOWN_SESSION",
                    f"unknown session {session_id}",
                )
            session.state.vx_mps = float(vx_mps)
            session.state.yaw_rate_rps = float(yaw_rate_rps)
            session.state.body_height_m = float(body_height_m)
            state = session.state.to_dict()
        return {"applied": True, "state": state}

    def teleop_state(self, *, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise IsaacRuntimeError(
                    "ISAAC_UNKNOWN_SESSION",
                    f"unknown session {session_id}",
                )
            state = session.state.to_dict()
            uptime_s = max(0.0, time.time() - session.started_at_s)
        return {"state": state, "uptime_s": uptime_s}

    def teleop_stop(self, *, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return {"stopped": True, "already_stopped": True}
        return {"stopped": True}

    def screenshot(
        self,
        *,
        width: int = 1280,
        height: int = 720,
        camera_position: list[float] | None = None,
        camera_target: list[float] | None = None,
    ) -> dict[str, Any]:
        """Capture the Isaac Sim viewport as a base64-encoded PNG.

        If *camera_position* / *camera_target* are provided, the active
        viewport camera is repositioned before capture.  All viewport
        operations are dispatched to the main thread.
        """
        if not self._engine.available:
            raise IsaacRuntimeError(
                "ISAAC_NOT_AVAILABLE",
                "Isaac Sim is not available — cannot capture viewport.",
            )

        def _do_screenshot() -> dict[str, Any]:
            from omni.kit.viewport.utility import (  # type: ignore[import-not-found]
                capture_viewport_to_file,
                get_active_viewport,
            )

            viewport = get_active_viewport()
            if viewport is None:
                raise IsaacRuntimeError(
                    "VIEWPORT_NOT_AVAILABLE",
                    "No active viewport — is the renderer enabled?",
                )

            # Optionally reposition camera
            if camera_position is not None or camera_target is not None:
                try:
                    from pxr import Gf  # type: ignore[import-not-found]
                    import omni.usd  # type: ignore[import-not-found]

                    stage = omni.usd.get_context().get_stage()
                    cam_path = viewport.get_active_camera()
                    cam_prim = stage.GetPrimAtPath(cam_path)
                    if cam_prim.IsValid():
                        from pxr import UsdGeom  # type: ignore[import-not-found]
                        xformable = UsdGeom.Xformable(cam_prim)
                        if camera_position is not None:
                            pos = Gf.Vec3d(*camera_position)
                            xformable.ClearXformOpOrder()
                            xformable.AddTranslateOp().Set(pos)
                        if camera_target is not None:
                            # Point camera at target using look-at
                            cam_pos = camera_position or [0.0, 0.0, 0.0]
                            _set_camera_look_at(
                                cam_prim,
                                Gf.Vec3d(*cam_pos),
                                Gf.Vec3d(*camera_target),
                            )
                except Exception as exc:
                    logger.warning("[runtime] screenshot: camera reposition failed (non-fatal): %s", exc)

            # Resize viewport
            try:
                viewport.set_texture_resolution((width, height))
            except Exception as exc:
                logger.warning("[runtime] screenshot: set_texture_resolution failed: %s", exc)

            # Pump a few frames so the viewport renders the current state
            try:
                app = self._engine.sim_app
                if app is not None:
                    for _ in range(4):
                        app.update()
            except Exception:
                pass

            # Capture to temp file, read back, base64 encode
            tmp_path = os.path.join(tempfile.gettempdir(), f"isaac_screenshot_{uuid.uuid4().hex[:8]}.png")
            try:
                capture_viewport_to_file(viewport, tmp_path)

                # The capture may be async — pump a few more frames and wait
                if self._engine.sim_app is not None:
                    for _ in range(8):
                        self._engine.sim_app.update()
                        if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
                            break

                if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) == 0:
                    raise IsaacRuntimeError(
                        "SCREENSHOT_FAILED",
                        f"Viewport capture produced no file at {tmp_path}",
                    )

                with open(tmp_path, "rb") as f:
                    png_bytes = f.read()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            image_b64 = base64.b64encode(png_bytes).decode("ascii")
            return {
                "image_base64": image_b64,
                "mime_type": "image/png",
                "width": width,
                "height": height,
            }

        return main_thread_dispatcher.submit(_do_screenshot)


def _set_camera_look_at(cam_prim: Any, eye: Any, target: Any) -> None:
    """Point a USD camera prim from *eye* toward *target*."""
    from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

    forward = (target - eye).GetNormalized()
    up = Gf.Vec3d(0, 0, 1)
    # If forward is nearly parallel to up, pick a different up
    if abs(Gf.Dot(forward, up)) > 0.99:
        up = Gf.Vec3d(0, 1, 0)
    right = Gf.Cross(forward, up).GetNormalized()
    new_up = Gf.Cross(right, forward).GetNormalized()

    mat = Gf.Matrix4d()
    mat.SetRow(0, Gf.Vec4d(right[0], right[1], right[2], 0))
    mat.SetRow(1, Gf.Vec4d(new_up[0], new_up[1], new_up[2], 0))
    mat.SetRow(2, Gf.Vec4d(-forward[0], -forward[1], -forward[2], 0))
    mat.SetRow(3, Gf.Vec4d(eye[0], eye[1], eye[2], 1))

    xformable = UsdGeom.Xformable(cam_prim)
    xformable.ClearXformOpOrder()
    xformable.AddTransformOp().Set(mat)


def _validate_mechanism(mechanism: dict[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(mechanism, dict):
        raise IsaacRuntimeError("INVALID_MECHANISM", "mechanism must be an object")
    parts = mechanism.get("parts")
    joints = mechanism.get("joints")
    if not isinstance(parts, list):
        raise IsaacRuntimeError("INVALID_MECHANISM", "mechanism.parts must be an array")
    if not isinstance(joints, list):
        raise IsaacRuntimeError("INVALID_MECHANISM", "mechanism.joints must be an array")
    return mechanism


def _unsupported_joints(mechanism: dict[str, Any]) -> list[dict[str, str]]:
    unsupported: list[dict[str, str]] = []
    joints = mechanism.get("joints", [])
    if not isinstance(joints, list):
        return unsupported
    for index, joint in enumerate(joints):
        if not isinstance(joint, dict):
            unsupported.append({"id": f"index_{index}", "joint_type": "unknown"})
            continue
        joint_type = str(joint.get("joint_type", "")).strip().lower()
        if joint_type not in SUPPORTED_JOINT_TYPES:
            joint_id = str(joint.get("id", f"index_{index}"))
            unsupported.append({"id": joint_id, "joint_type": joint_type or "unknown"})
    return unsupported


def _validate_finite(name: str, value: Any) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise IsaacRuntimeError("INVALID_INPUT", f"{name} must be a finite number")
    return float(value)


def _validate_sim_args(*, duration_s: float, dt_s: float, output_interval: float) -> None:
    duration = _validate_finite("duration_s", duration_s)
    dt = _validate_finite("dt_s", dt_s)
    out = _validate_finite("output_interval", output_interval)
    if duration <= 0:
        raise IsaacRuntimeError("INVALID_INPUT", "duration_s must be > 0")
    if dt <= 0:
        raise IsaacRuntimeError("INVALID_INPUT", "dt_s must be > 0")
    if out <= 0:
        raise IsaacRuntimeError("INVALID_INPUT", "output_interval must be > 0")
    if out < dt:
        raise IsaacRuntimeError("INVALID_INPUT", "output_interval must be >= dt_s")
    if out > duration:
        raise IsaacRuntimeError("INVALID_INPUT", "output_interval must be <= duration_s")


def _sample_times(*, duration_s: float, output_interval: float) -> list[float]:
    n = max(1, int(math.floor(duration_s / output_interval)))
    out = [round(i * output_interval, 9) for i in range(n + 1)]
    if out[-1] < duration_s:
        out.append(round(duration_s, 9))
    else:
        out[-1] = round(duration_s, 9)
    return out


def _steady_state_speeds(mechanism: dict[str, Any]) -> dict[str, float]:
    part_ids = [
        p.get("id")
        for p in mechanism.get("parts", [])
        if isinstance(p, dict) and isinstance(p.get("id"), str)
    ]
    speeds = {pid: 0.0 for pid in part_ids if isinstance(pid, str)}
    drives = mechanism.get("drives", [])
    joints = mechanism.get("joints", [])
    if not isinstance(drives, list) or not isinstance(joints, list):
        return speeds
    joint_by_id = {
        str(j.get("id")): j
        for j in joints
        if isinstance(j, dict) and isinstance(j.get("id"), str)
    }
    for drive in drives:
        if not isinstance(drive, dict):
            continue
        speed = drive.get("speed_rpm")
        if not isinstance(speed, (int, float)) or not math.isfinite(float(speed)):
            continue
        joint_id = drive.get("joint_id")
        if not isinstance(joint_id, str):
            continue
        joint = joint_by_id.get(joint_id)
        if not isinstance(joint, dict):
            continue
        child = joint.get("child_part")
        parent = joint.get("parent_part")
        if isinstance(child, str):
            speeds[child] = float(speed)
        if isinstance(parent, str) and parent not in speeds:
            speeds[parent] = float(speed)
    return speeds
