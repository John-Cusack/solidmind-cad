"""Isaac bridge runtime.

The runtime exposes deterministic command handlers for the bridge protocol.
When Omniverse Isaac APIs are available, a minimal physics stepping path is used.
Otherwise, a deterministic analytical fallback is used for local/CI execution.
"""
from __future__ import annotations

import base64
import importlib
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

from isaac_bridge.controllers import clamp_targets, create_controller
from isaac_bridge.models import (
    SUPPORTED_JOINT_TYPES,
    SimulationSession,
    TeleopConfig,
    TeleopConfigError,
    TeleopState,
    URDFImportConfig,
)

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
            import omni.usd  # type: ignore[import-not-found]
            from pxr import Gf, UsdLux, UsdPhysics  # type: ignore[import-not-found]

            stage = omni.usd.get_context().get_stage()

            # Physics scene
            scene_path = "/World/PhysicsScene"
            if not stage.GetPrimAtPath(scene_path).IsValid():
                scene_prim = stage.DefinePrim(scene_path, "PhysicsScene")
                UsdPhysics.Scene(scene_prim).CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
                UsdPhysics.Scene(scene_prim).CreateGravityMagnitudeAttr(9.81)
            else:
                scene_prim = stage.GetPrimAtPath(scene_path)

            # Configure PhysX solver: TGS with higher iteration counts and
            # clamped depenetration velocity.  Non-fatal if PhysxSchema is
            # unavailable (e.g. in CI without full Omniverse stack).
            try:
                from pxr import PhysxSchema  # type: ignore[import-not-found]
                physx_api = PhysxSchema.PhysxSceneAPI.Apply(scene_prim)
                physx_api.CreateSolverTypeAttr("TGS")
                physx_api.CreateMaxDepenetrationVelocityAttr(5.0)
                # Position/velocity iterations for TGS convergence
                _attrs = dir(physx_api)
                if "CreateMinPositionIterationCountAttr" in _attrs:
                    physx_api.CreateMinPositionIterationCountAttr(8)
                if "CreateMinVelocityIterationCountAttr" in _attrs:
                    physx_api.CreateMinVelocityIterationCountAttr(4)
                logger.info("[engine] setup_scene: TGS solver configured (max_depen_vel=5.0)")
            except Exception as exc:
                logger.warning("[engine] setup_scene: PhysX solver config failed (non-fatal): %s", exc)

            # Ground plane
            ground_path = "/World/GroundPlane"
            if not stage.GetPrimAtPath(ground_path).IsValid():
                from omni.isaac.core.objects import GroundPlane  # type: ignore[import-not-found]
                GroundPlane(prim_path=ground_path)

            # Ground plane physics material with friction.  Non-fatal if
            # UsdShade or PhysxSchema are unavailable.
            try:
                from pxr import PhysxSchema, UsdShade  # type: ignore[import-not-found]

                mat_path = "/World/GroundMaterial"
                if not stage.GetPrimAtPath(mat_path).IsValid():
                    mat_prim = stage.DefinePrim(mat_path, "Material")
                    UsdPhysics.MaterialAPI.Apply(mat_prim)
                    phys_mat = UsdPhysics.MaterialAPI(mat_prim)
                    phys_mat.CreateStaticFrictionAttr(1.0)
                    phys_mat.CreateDynamicFrictionAttr(0.8)
                    phys_mat.CreateRestitutionAttr(0.0)

                # Bind material to ground plane collision geometry
                ground_prim = stage.GetPrimAtPath(ground_path)
                if ground_prim.IsValid():
                    # The GroundPlane helper may nest collision geometry;
                    # bind to the root and let USD inherit downward.
                    binding_api = UsdShade.MaterialBindingAPI.Apply(ground_prim)
                    mat_prim = stage.GetPrimAtPath(mat_path)
                    binding_api.Bind(
                        UsdShade.Material(mat_prim),
                        UsdShade.Tokens.weakerThanDescendants,
                        "physics",
                    )
                logger.info("[engine] setup_scene: ground friction material applied (static=1.0, dynamic=0.8)")
            except Exception as exc:
                logger.warning("[engine] setup_scene: ground material config failed (non-fatal): %s", exc)

            # Distant light (direct illumination — key light from above-right)
            light_path = "/World/DistantLight"
            if not stage.GetPrimAtPath(light_path).IsValid():
                light_prim = stage.DefinePrim(light_path, "DistantLight")
                dlight = UsdLux.DistantLight(light_prim)
                dlight.CreateIntensityAttr(5000)
                dlight.CreateAngleAttr(1.0)  # soft shadows
                from pxr import UsdGeom  # type: ignore[import-not-found]
                UsdGeom.Xformable(light_prim).AddRotateXYZOp().Set(Gf.Vec3f(-45, 30, 0))

            # Dome light (ambient/indirect illumination — essential for RTX
            # renderer to make objects visible without an environment USD)
            dome_path = "/World/DomeLight"
            if not stage.GetPrimAtPath(dome_path).IsValid():
                dome_prim = stage.DefinePrim(dome_path, "DomeLight")
                dome = UsdLux.DomeLight(dome_prim)
                dome.CreateIntensityAttr(3000)
                dome.CreateColorAttr(Gf.Vec3f(0.9, 0.92, 1.0))  # neutral sky

            # Ground plane visual material (grey so robot is visible against it)
            try:
                from pxr import Sdf, UsdShade  # type: ignore[import-not-found]
                ground_vis_mat_path = "/World/GroundVisualMaterial"
                if not stage.GetPrimAtPath(ground_vis_mat_path).IsValid():
                    gvm = stage.DefinePrim(ground_vis_mat_path, "Material")
                    gv_shader = stage.DefinePrim(f"{ground_vis_mat_path}/Shader", "Shader")
                    gvs = UsdShade.Shader(gv_shader)
                    gvs.CreateIdAttr("UsdPreviewSurface")
                    gvs.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.5, 0.5, 0.5))
                    gvs.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.8)
                    UsdShade.Material(gvm).CreateSurfaceOutput().ConnectToSource(gvs.ConnectableAPI(), "surface")
                    # Bind to ground plane
                    gp = stage.GetPrimAtPath("/World/GroundPlane")
                    if gp.IsValid():
                        UsdShade.MaterialBindingAPI.Apply(gp).Bind(
                            UsdShade.Material(gvm),
                            UsdShade.Tokens.weakerThanDescendants,
                        )
                logger.info("[engine] setup_scene: ground visual material applied")
            except Exception as exc:
                logger.warning("[engine] setup_scene: ground visual material failed: %s", exc)

            self._scene_ready = True
            logger.info("[engine] setup_scene: done")
        except Exception as exc:
            logger.warning("[engine] setup_scene failed (non-fatal): %s", exc)

    def load_environment(self, usd_url: str, *, skip_ground_plane: bool = True) -> str:
        """Reference an external USD environment into the stage.

        Args:
            usd_url: Local path or Omniverse/S3 URL to a USD file.
            skip_ground_plane: If True, remove the default ground plane
                (the environment typically provides its own floor).

        Returns:
            The prim path where the environment was loaded.
        """
        if not self._available:
            raise IsaacRuntimeError(
                "ENGINE_UNAVAILABLE",
                "Isaac Sim engine not available",
            )

        def _do_load() -> str:
            import omni.usd  # type: ignore[import-not-found]

            stage = omni.usd.get_context().get_stage()
            env_path = "/Environment"

            # Remove existing environment prim if present
            existing = stage.GetPrimAtPath(env_path)
            if existing.IsValid():
                stage.RemovePrim(env_path)

            # Create an Xform and add the USD as a reference
            env_prim = stage.DefinePrim(env_path, "Xform")
            env_prim.GetReferences().AddReference(usd_url)
            logger.info("[engine] load_environment: referenced %s at %s", usd_url, env_path)

            # Optionally remove the default ground plane (environment has its own)
            if skip_ground_plane:
                gp = stage.GetPrimAtPath("/World/GroundPlane")
                if gp.IsValid():
                    stage.RemovePrim("/World/GroundPlane")
                    logger.info("[engine] load_environment: removed default ground plane")

            self._imported_prims.append(env_path)
            return env_path

        return main_thread_dispatcher.submit(_do_load)

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

        # Remove any stale prim at the expected import path to avoid
        # "name is not unique" errors on re-import.
        _expected_name = os.path.splitext(os.path.basename(urdf_path))[0]
        _expected_path = f"/{_expected_name}"
        _stage = omni.usd.get_context().get_stage()
        _existing = _stage.GetPrimAtPath(_expected_path)
        if _existing.IsValid():
            logger.info("[engine] import_urdf: removing stale prim at %s", _expected_path)
            _stage.RemovePrim(_expected_path)

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

        # Override drive stiffness/damping on all joint prims to match config.
        # The URDF importer may apply its own internal defaults that differ
        # from the config values (especially for stiffness/damping), so we
        # walk all joints and force-set the values post-import.
        self._configure_drives_post_import(prim_path, config)

        # Ensure collision geometry exists on every rigid-body link.
        # The in-memory-stage URDF importer sometimes creates empty
        # collision Xforms without actual mesh prims inside, causing
        # the robot to fall through the ground plane.
        self._ensure_collision_geometry(prim_path, urdf_path=urdf_path)

        # Apply a visible display color to all mesh prims (URDF importer
        # doesn't apply material colors, making the robot invisible).
        self._apply_display_color(prim_path)

        # Auto-frame the viewport camera on the imported model
        self._frame_camera_on_prim(prim_path)

        logger.info(
            "[engine] import_urdf: prim type census: %s",
            dict(sorted(type_name_counts.items())),
        )
        logger.info(
            "[engine] import_urdf: done in %.3fs — prim=%s joints=%d links=%d",
            time.monotonic() - t0, prim_path, joint_count, link_count,
        )
        return prim_path, joint_count, link_count

    def _apply_display_color(self, prim_path: str) -> None:
        """Create an OmniPBR material and bind it to all Mesh prims under prim_path.

        The URDF importer creates meshes without materials, making the robot
        invisible in RTX rendering.  We create a single bright orange OmniPBR
        material and bind it to every mesh.
        """
        try:
            import omni.usd  # type: ignore[import-not-found]
            from pxr import Gf, Sdf, UsdGeom, UsdShade, Vt  # type: ignore[import-not-found]

            stage = omni.usd.get_context().get_stage()
            root = stage.GetPrimAtPath(prim_path)
            if not root.IsValid():
                logger.warning("[engine] _apply_display_color: root prim invalid")
                return

            # Create a simple USD Preview Surface material
            mat_path = f"{prim_path}/RobotMaterial"
            mat_prim = stage.DefinePrim(mat_path, "Material")
            material = UsdShade.Material(mat_prim)

            # Create shader
            shader_path = f"{mat_path}/Shader"
            shader_prim = stage.DefinePrim(shader_path, "Shader")
            shader = UsdShade.Shader(shader_prim)
            shader.CreateIdAttr("UsdPreviewSurface")
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(1.0, 0.4, 0.0))
            shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.4)
            shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)

            # Connect material output to shader
            material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")

            # Walk subtree and bind material to all meshes
            count = 0
            stack = [root]
            while stack:
                p = stack.pop()
                if p.GetTypeName() == "Mesh":
                    binding = UsdShade.MaterialBindingAPI.Apply(p)
                    binding.Bind(material)
                    # Also set displayColor as fallback
                    gprim = UsdGeom.Gprim(p)
                    gprim.CreateDisplayColorAttr().Set(Vt.Vec3fArray([Gf.Vec3f(1.0, 0.4, 0.0)]))
                    count += 1
                for child in p.GetChildren():
                    stack.append(child)

            logger.info("[engine] _apply_display_color: bound material to %d meshes under %s", count, prim_path)
        except Exception as exc:
            logger.warning("[engine] _apply_display_color failed (non-fatal): %s", exc)

    def _frame_camera_on_prim(self, prim_path: str) -> None:
        """Position the viewport camera to frame an imported prim.

        Computes the world bounding box of the prim, then places the
        camera at a 45° isometric angle at 2.5× the bounding sphere
        radius, looking at the center.
        """
        try:
            import omni.usd  # type: ignore[import-not-found]
            from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

            stage = omni.usd.get_context().get_stage()
            prim = stage.GetPrimAtPath(prim_path)
            if not prim.IsValid():
                return

            # Compute world-space bounding box
            bbox_cache = UsdGeom.BBoxCache(0.0, [UsdGeom.Tokens.default_])
            bbox = bbox_cache.ComputeWorldBound(prim)
            box_range = bbox.ComputeAlignedRange()
            if box_range.IsEmpty():
                logger.debug("[engine] _frame_camera: bbox empty for %s", prim_path)
                return

            center = (Gf.Vec3d(box_range.GetMin()) + Gf.Vec3d(box_range.GetMax())) / 2.0
            size = Gf.Vec3d(box_range.GetMax()) - Gf.Vec3d(box_range.GetMin())
            radius = size.GetLength() / 2.0
            if radius < 1e-6:
                return

            # Place camera at isometric angle, 2.5× bounding sphere radius
            dist = radius * 2.5
            eye = center + Gf.Vec3d(dist * 0.577, dist * 0.577, dist * 0.577)
            target = center

            from omni.kit.viewport.utility import (
                get_active_viewport,  # type: ignore[import-not-found]
            )
            viewport = get_active_viewport()
            if viewport is not None:
                _reposition_camera(
                    viewport,
                    [eye[0], eye[1], eye[2]],
                    [target[0], target[1], target[2]],
                    self.sim_app,
                )
                logger.info(
                    "[engine] _frame_camera: framed on %s — center=%s radius=%.3f",
                    prim_path, center, radius,
                )
            else:
                logger.debug("[engine] _frame_camera: no active viewport")
        except Exception as exc:
            logger.warning("[engine] _frame_camera failed (non-fatal): %s", exc)

    def _configure_drives_post_import(
        self,
        prim_path: str,
        config: URDFImportConfig,
    ) -> None:
        """Override drive stiffness/damping on all joints after URDF import.

        The URDF importer's internal defaults may not match the values
        requested in *config*.  This walks every joint prim under the
        imported root and sets ``stiffness`` and ``damping`` on the
        ``UsdPhysics.DriveAPI`` (angular for revolute, linear for prismatic).
        """
        try:
            import omni.usd  # type: ignore[import-not-found]
            from pxr import Usd, UsdPhysics  # type: ignore[import-not-found]

            stage = omni.usd.get_context().get_stage()
            root_prim = stage.GetPrimAtPath(prim_path)
            if not root_prim.IsValid():
                return

            configured = 0
            for prim in Usd.PrimRange(root_prim):
                type_name = prim.GetTypeName()
                if type_name not in _JOINT_TYPE_NAMES:
                    continue

                # Determine drive axis: angular for revolute/spherical, linear for prismatic.
                if "Prismatic" in type_name:
                    drive_token = "linear"
                else:
                    drive_token = "angular"

                drive_api = UsdPhysics.DriveAPI.Get(prim, drive_token)
                if not drive_api:
                    continue

                drive_api.GetStiffnessAttr().Set(config.default_drive_stiffness)
                drive_api.GetDampingAttr().Set(config.default_drive_damping)
                configured += 1

            logger.info(
                "[engine] _configure_drives_post_import: set stiffness=%.1f damping=%.1f on %d joints",
                config.default_drive_stiffness,
                config.default_drive_damping,
                configured,
            )
        except Exception as exc:
            logger.warning("[engine] _configure_drives_post_import failed (non-fatal): %s", exc)

    def _ensure_collision_geometry(
        self, prim_path: str, urdf_path: str | None = None,
    ) -> None:
        """Add collision boxes to links whose collision Xforms are empty.

        The in-memory-stage URDF importer sometimes creates placeholder
        ``/link/collisions`` Xforms without any child mesh.  This walks
        every link with ``PhysicsRigidBodyAPI``, checks if its collision
        container is empty, and if so creates a ``UsdGeom.Cube`` with
        ``PhysicsCollisionAPI`` sized to match the link's geometry.

        When *urdf_path* is given, the URDF is parsed to extract collision
        mesh filenames and their ``<origin>`` transforms.  The actual STL
        files are read (binary or ASCII) to compute real bounding-box
        extents, so colliders are correctly sized even when the in-memory
        stage can't resolve mesh references.
        """
        try:
            import omni.usd  # type: ignore[import-not-found]
            from pxr import Gf, Usd, UsdGeom, UsdPhysics  # type: ignore[import-not-found]

            stage = omni.usd.get_context().get_stage()
            root_prim = stage.GetPrimAtPath(prim_path)
            if not root_prim.IsValid():
                return

            # Build link→(center, half_extents) from URDF collision meshes.
            urdf_col: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {}
            fixed_child_links: set[str] = set()
            if urdf_path:
                urdf_col = _parse_urdf_collision_extents(urdf_path)
                if urdf_col:
                    logger.info(
                        "[engine] _ensure_collision_geometry: parsed %d link collision extents from URDF",
                        len(urdf_col),
                    )
                # Identify links that are children of fixed joints — these are
                # co-located with their parent link (e.g. servo bodies merged
                # into structural links).  Adding separate collision boxes to
                # them causes self-collision with the parent's collider.
                try:
                    import xml.etree.ElementTree as _ET
                    _tree = _ET.parse(urdf_path)
                    for _j in _tree.findall(".//joint"):
                        if _j.get("type") == "fixed":
                            _child = _j.find("child")
                            if _child is not None:
                                _cname = _child.get("link", "")
                                if _cname:
                                    fixed_child_links.add(_cname)
                    if fixed_child_links:
                        logger.info(
                            "[engine] _ensure_collision_geometry: skipping %d fixed-joint child links: %s",
                            len(fixed_child_links),
                            ", ".join(sorted(fixed_child_links)[:6]) + ("..." if len(fixed_child_links) > 6 else ""),
                        )
                except Exception as _exc:
                    logger.debug("[engine] failed to parse fixed joints from URDF: %s", _exc)

            added = 0
            for prim in Usd.PrimRange(root_prim):
                if not prim.HasAPI(UsdPhysics.RigidBodyAPI):
                    continue

                # Check if there are any child prims with collision API already
                has_collider = False
                for child in Usd.PrimRange(prim):
                    if child == prim:
                        continue
                    if child.HasAPI(UsdPhysics.CollisionAPI):
                        has_collider = True
                        break
                if has_collider:
                    continue

                link_name = prim.GetName()

                # Skip chassis / base_link — their large collision boxes
                # catch the ground on any slight tip and cascade into a fall.
                # Only leg links need ground-contact colliders.
                _skip = ("base_link", "chassis", "Body_Chassis")
                if link_name in _skip:
                    continue

                # Skip fixed-joint child links (e.g. servo bodies) — they
                # are co-located with their parent and would self-collide.
                if link_name in fixed_child_links:
                    continue

                # Try URDF-derived extents first, fall back to bbox, then default
                if link_name in urdf_col:
                    center_t, half_ext = urdf_col[link_name]
                    center = Gf.Vec3d(*center_t)
                    sx = max(half_ext[0], 0.005)
                    sy = max(half_ext[1], 0.005)
                    sz = max(half_ext[2], 0.005)
                else:
                    bbox_cache = UsdGeom.BBoxCache(0.0, [UsdGeom.Tokens.default_])
                    bbox = bbox_cache.ComputeLocalBound(prim)
                    box_range = bbox.ComputeAlignedRange()
                    if not box_range.IsEmpty():
                        mn = Gf.Vec3d(box_range.GetMin())
                        mx = Gf.Vec3d(box_range.GetMax())
                        center = (mn + mx) / 2.0
                        sx = max(float(mx[0] - mn[0]) / 2.0, 0.005)
                        sy = max(float(mx[1] - mn[1]) / 2.0, 0.005)
                        sz = max(float(mx[2] - mn[2]) / 2.0, 0.005)
                    else:
                        center = Gf.Vec3d(0, 0, 0)
                        sx = sy = sz = 0.01

                # Enforce minimum thickness so thin links don't tunnel
                _MIN_HALF = 0.008  # 8mm minimum half-extent
                sx = max(sx, _MIN_HALF)
                sy = max(sy, _MIN_HALF)
                sz = max(sz, _MIN_HALF)

                # Create collision cube.
                # UsdGeom.Cube size=2 gives half-extent=1, so scale directly
                # maps to half-extent in meters.
                col_path = prim.GetPath().AppendChild("auto_collision")
                cube = UsdGeom.Cube.Define(stage, col_path)
                cube.CreateSizeAttr(2.0)
                cube.AddScaleOp().Set(Gf.Vec3f(sx, sy, sz))
                cube.AddTranslateOp().Set(Gf.Vec3d(center[0], center[1], center[2]))
                cube.CreatePurposeAttr("guide")

                col_prim = stage.GetPrimAtPath(col_path)
                UsdPhysics.CollisionAPI.Apply(col_prim)
                try:
                    from pxr import PhysxSchema  # type: ignore[import-not-found]
                    PhysxSchema.PhysxCollisionAPI.Apply(col_prim)
                except Exception:
                    pass

                # For tibia links, add a foot sphere at the tip for ground contact.
                if "tibia" in link_name.lower():
                    foot_path = prim.GetPath().AppendChild("foot_collision")
                    foot_radius = 0.015  # 15mm radius foot sphere
                    sphere = UsdGeom.Sphere.Define(stage, foot_path)
                    sphere.CreateRadiusAttr(foot_radius)
                    # Place at the tip of the tibia (max X in link-local frame)
                    tip_x = float(center[0]) + sx  # end of the link
                    sphere.AddTranslateOp().Set(Gf.Vec3d(tip_x, 0.0, 0.0))
                    sphere.CreatePurposeAttr("guide")
                    foot_prim = stage.GetPrimAtPath(foot_path)
                    UsdPhysics.CollisionAPI.Apply(foot_prim)
                    try:
                        from pxr import PhysxSchema  # type: ignore[import-not-found]
                        PhysxSchema.PhysxCollisionAPI.Apply(foot_prim)
                    except Exception:
                        pass

                added += 1
                logger.debug(
                    "[engine] _ensure_collision_geometry: %s half_ext=(%.4f, %.4f, %.4f) center=(%.4f, %.4f, %.4f)",
                    link_name, sx, sy, sz, center[0], center[1], center[2],
                )

            if added > 0:
                logger.info(
                    "[engine] _ensure_collision_geometry: added %d collision boxes to links under %s",
                    added, prim_path,
                )
            else:
                logger.debug("[engine] _ensure_collision_geometry: all links already have colliders")
        except Exception as exc:
            logger.warning("[engine] _ensure_collision_geometry failed (non-fatal): %s", exc)

    def _set_initial_positions_usd(
        self,
        prim_path: str,
        positions_deg: dict[str, float],
    ) -> int:
        """Set drive target positions AND JointStateAPI on USD prims pre-reset.

        This runs BEFORE world.reset() / create_articulation() so the physics
        engine sees the correct initial configuration from the very first step.
        Uses PhysxSchema.JointStateAPI which persists through world.reset().
        """
        if not positions_deg:
            return 0

        import omni.usd  # type: ignore[import-not-found]
        from pxr import PhysxSchema, Usd, UsdPhysics  # type: ignore[import-not-found]

        stage = omni.usd.get_context().get_stage()
        root_prim = stage.GetPrimAtPath(prim_path)
        if not root_prim.IsValid():
            return 0

        configured = 0
        for prim in Usd.PrimRange(root_prim):
            type_name = prim.GetTypeName()
            if type_name not in _JOINT_TYPE_NAMES:
                continue
            prim_name = prim.GetName()
            for joint_name, angle_deg in positions_deg.items():
                if joint_name == prim_name or prim_name.endswith(joint_name):
                    # 1. Set drive target (PD controller goal)
                    drive_api = UsdPhysics.DriveAPI.Get(prim, "angular")
                    if drive_api:
                        drive_api.GetTargetPositionAttr().Set(angle_deg)

                    # 2. Set JointStateAPI (actual initial physics position)
                    try:
                        joint_state = PhysxSchema.JointStateAPI.Apply(
                            prim, UsdPhysics.Tokens.angular
                        )
                        joint_state.CreatePositionAttr(angle_deg)
                        joint_state.CreateVelocityAttr(0.0)
                    except Exception as exc:
                        logger.warning(
                            "[engine] JointStateAPI.Apply failed for %s (non-fatal): %s",
                            prim_name, exc,
                        )

                    configured += 1
                    break

        logger.info(
            "[engine] _set_initial_positions_usd: set %d drive targets + JointStateAPI attrs",
            configured,
        )
        return configured

    def set_initial_joint_positions(
        self,
        prim_path: str,
        articulation: Any,
        positions_deg: dict[str, float],
    ) -> int:
        """Set initial joint positions on USD drive targets and articulation default state.

        Uses set_joints_default_state() instead of set_joint_positions() so that
        positions survive world.reset() calls.

        Must be called after create_articulation() and before the final world.reset().
        Returns the number of USD drive targets configured.
        """
        if not positions_deg:
            return 0

        import omni.usd  # type: ignore[import-not-found]
        from pxr import Usd, UsdPhysics  # type: ignore[import-not-found]

        stage = omni.usd.get_context().get_stage()
        root_prim = stage.GetPrimAtPath(prim_path)

        configured = 0
        if root_prim.IsValid():
            # Set drive target positions on USD prims
            for prim in Usd.PrimRange(root_prim):
                type_name = prim.GetTypeName()
                if type_name not in _JOINT_TYPE_NAMES:
                    continue
                prim_name = prim.GetName()
                for joint_name, angle_deg in positions_deg.items():
                    if joint_name == prim_name or prim_name.endswith(joint_name):
                        drive_api = UsdPhysics.DriveAPI.Get(prim, "angular")
                        if drive_api:
                            drive_api.GetTargetPositionAttr().Set(angle_deg)
                            configured += 1
                        break

        # Set articulation default state (survives world.reset())
        name_to_rad = {k: math.radians(v) for k, v in positions_deg.items()}
        try:
            import numpy as np  # type: ignore[import-not-found]
            joint_names = articulation.dof_names
            if joint_names:
                current_pos = articulation.get_joint_positions()
                if current_pos is not None:
                    new_pos = np.array(current_pos, dtype=np.float32)
                    for i, jn in enumerate(joint_names):
                        if jn in name_to_rad:
                            new_pos[i] = name_to_rad[jn]
                    num_dof = len(new_pos)
                    articulation.set_joints_default_state(
                        positions=new_pos,
                        velocities=np.zeros(num_dof, dtype=np.float32),
                        efforts=np.zeros(num_dof, dtype=np.float32),
                    )
                    logger.info(
                        "[engine] set_initial_joint_positions: set default state for %d DOFs",
                        num_dof,
                    )
        except Exception as exc:
            logger.warning(
                "[engine] set_joints_default_state failed (non-fatal): %s", exc
            )

        logger.info(
            "[engine] set_initial_joint_positions: configured %d drive targets",
            configured,
        )
        return configured

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
        from pxr import Usd, UsdPhysics  # type: ignore[import-not-found]

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
        skip_mechanism_drives: bool = False,
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

            # Phase 0: Raise root prim so the robot spawns above the ground.
            # For mobile robots the URDF root is at z=0 but the standing
            # configuration has feet at z=-ground_clearance.  Without this
            # offset the legs start underground and the robot collapses.
            logger.info("[engine] start_simulation: config.spawn_height=%r", getattr(config, 'spawn_height', 'MISSING'))
            if getattr(config, 'spawn_height', 0) and abs(config.spawn_height) > 1e-6:
                try:
                    import omni.usd  # type: ignore[import-not-found]
                    from pxr import Gf, UsdGeom  # type: ignore[import-not-found]
                    stage = omni.usd.get_context().get_stage()
                    root_prim = stage.GetPrimAtPath(pp)
                    logger.info("[engine] start_simulation: root_prim valid=%s path=%s", root_prim.IsValid(), pp)
                    if root_prim.IsValid():
                        xformable = UsdGeom.Xformable(root_prim)
                        # URDF importer already creates xformOp:translate —
                        # modify the existing op's z instead of adding a new one.
                        translate_set = False
                        for op in xformable.GetOrderedXformOps():
                            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                                cur = op.Get()
                                op.Set(Gf.Vec3d(cur[0], cur[1], cur[2] + config.spawn_height))
                                translate_set = True
                                break
                        if not translate_set:
                            xformable.AddTranslateOp().Set(Gf.Vec3d(0, 0, config.spawn_height))
                        logger.info(
                            "[engine] start_simulation: spawn_height=%.4f applied on %s",
                            config.spawn_height, pp,
                        )
                except Exception as exc:
                    logger.warning("[engine] start_simulation: spawn_height failed: %s", exc, exc_info=True)

            # Phase 1: Set JointStateAPI + drive targets on USD (before world.reset)
            if config.initial_joint_positions:
                n = self._set_initial_positions_usd(pp, config.initial_joint_positions)
                logger.info("[engine] start_simulation: set %d USD initial positions pre-reset", n)

            # Phase 2: create_articulation() calls world.reset() internally
            art = self.create_articulation(pp)

            # Phase 3: Set default state on articulation (survives future resets)
            if config.initial_joint_positions:
                self.set_initial_joint_positions(pp, art, config.initial_joint_positions)

                # Reset again — world.reset() re-initializes physics handles,
                # reads JointStateAPI attrs from USD, and restores default state
                # set by set_joints_default_state(). Unlike timeline.stop()/play(),
                # this keeps the articulation handle valid for subsequent stepping.
                self.world.reset()
                logger.info("[engine] start_simulation: second world.reset() with default state")

                # Log what the articulation sees after reset
                try:
                    dof_names = art.dof_names
                    cur_pos = art.get_joint_positions()
                    logger.info("[engine] start_simulation: DOF names: %s", dof_names)
                    if cur_pos is not None:
                        logger.info("[engine] start_simulation: joint positions after reset: %s",
                                    [f"{p:.1f}" for p in cur_pos])
                except Exception as exc:
                    logger.warning("[engine] start_simulation: position readback failed: %s", exc)

            # Phase 4: Re-apply drive stiffness/damping AFTER world.reset(),
            # which resets USD drive attributes to URDF importer defaults.
            self._configure_drives_post_import(pp, config)

            # Phase 5: Apply mechanism drives (skipped for teleop — teleop
            # needs position drives, but mechanism drives set velocity mode
            # with stiffness=0, which disables position control entirely).
            warns: list[str] = []
            if mechanism and not skip_mechanism_drives:
                logger.info("[engine] start_simulation: applying drives...")
                warns = self.apply_drives(pp, mechanism)
                if warns:
                    logger.warning("[engine] start_simulation: drive warnings: %s", warns)
            elif skip_mechanism_drives:
                logger.info("[engine] start_simulation: skipping mechanism drives (teleop mode)")
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
        Collects positions, velocities, and measured joint efforts (forces/torques).
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
                        # Extract measured joint efforts (forces/torques)
                        try:
                            efforts = articulation.get_measured_joint_efforts()
                            if efforts is not None:
                                sample["joint_efforts"] = [float(e) for e in efforts]
                        except Exception:
                            pass  # API may not be available in all Isaac versions
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

            # Clear the World properly before discarding it.
            if self._world is not None:
                try:
                    self._world.clear()
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
                        logger.info("[engine] cleanup: removing prim %s", prim_path)
                        stage.RemovePrim(prim_path)
                self._imported_prims.clear()
            except Exception as exc:
                logger.warning("[engine] cleanup: prim removal error: %s", exc)

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


def _reposition_camera(
    viewport: Any,
    eye: list[float],
    target: list[float],
    sim_app: Any,
) -> None:
    """Reposition the viewport camera to look from *eye* at *target*.

    Strategy (layered fallback):
    1. ``set_camera_view`` from isaacsim utilities — simplest, handles
       ViewportCameraState internally.
    2. ``Gf.Matrix4d.SetLookAt`` on the existing viewport camera prim —
       uses USD's built-in look-at math (avoids hand-rolled matrix bugs).
    3. Log warning and continue (screenshot still taken from default pose).
    """
    eye_tuple = tuple(eye)
    target_tuple = tuple(target)

    # ------------------------------------------------------------------
    # Strategy 1: isaacsim.core.utils.viewports.set_camera_view
    # ------------------------------------------------------------------
    try:
        import numpy as np  # type: ignore[import-not-found]
        from isaacsim.core.utils.viewports import set_camera_view  # type: ignore[import-not-found]

        cam_path = str(viewport.get_active_camera())
        set_camera_view(
            eye=np.array(eye_tuple),
            target=np.array(target_tuple),
            camera_prim_path=cam_path,
            viewport_api=viewport,
        )
        # Pump frames so the change propagates to the renderer
        if sim_app is not None:
            for _ in range(16):
                sim_app.update()
        logger.debug(
            "[runtime] camera repositioned via set_camera_view "
            "eye=%s target=%s cam=%s",
            eye_tuple, target_tuple, cam_path,
        )
        return
    except Exception as exc:
        logger.debug(
            "[runtime] set_camera_view unavailable, trying fallback: %s", exc,
        )

    # ------------------------------------------------------------------
    # Strategy 2: USD SetLookAt on existing viewport camera prim
    # ------------------------------------------------------------------
    try:
        import omni.usd  # type: ignore[import-not-found]
        from pxr import Gf, Sdf, UsdGeom  # type: ignore[import-not-found]

        stage = omni.usd.get_context().get_stage()
        cam_path = str(viewport.get_active_camera())
        cam_prim = stage.GetPrimAtPath(cam_path)
        if not cam_prim.IsValid():
            logger.warning("[runtime] camera prim %s not valid", cam_path)
            return

        eye_v = Gf.Vec3d(*eye_tuple)
        target_v = Gf.Vec3d(*target_tuple)
        up = Gf.Vec3d(0, 0, 1)
        # SetLookAt returns the *view* matrix; we need the inverse for
        # the world-space xform of the camera prim.
        view_mat = Gf.Matrix4d(1)
        view_mat.SetLookAt(eye_v, target_v, up)
        xform_mat = view_mat.GetInverse()

        xformable = UsdGeom.Xformable(cam_prim)
        xformable.ClearXformOpOrder()
        xformable.AddTransformOp().Set(xform_mat)

        # Set center-of-interest for viewport controller interop
        dist = (target_v - eye_v).GetLength()
        cam_prim.CreateAttribute(
            "omni:kit:centerOfInterest",
            Sdf.ValueTypeNames.Vector3d,
            custom=True,
        ).Set(Gf.Vec3d(0, 0, -dist))

        # Pump frames
        if sim_app is not None:
            for _ in range(16):
                sim_app.update()
        logger.debug(
            "[runtime] camera repositioned via SetLookAt "
            "eye=%s target=%s cam=%s",
            eye_tuple, target_tuple, cam_path,
        )
        return
    except Exception as exc:
        logger.warning(
            "[runtime] camera reposition failed (non-fatal): %s", exc,
        )


def _parse_urdf_collision_extents(
    urdf_path: str,
) -> dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]]:
    """Parse a URDF and compute collision box extents from STL meshes.

    Returns ``{link_name: (center_xyz, half_extents_xyz)}`` in metres.
    Each collision mesh's ``<origin>`` is applied, and the STL scale
    attribute is respected.  If an STL file can't be read, the link is
    skipped silently.
    """
    import struct
    import xml.etree.ElementTree as ET

    def _read_stl_extents(stl_path: str) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
        """Read an STL file (binary or ASCII) and return (min_xyz, max_xyz)."""
        try:
            with open(stl_path, "rb") as f:
                f.read(80)
                num_tri_bytes = f.read(4)
                if len(num_tri_bytes) < 4:
                    return None
                num_tris = struct.unpack("<I", num_tri_bytes)[0]
                # Quick heuristic: binary STL has 80 + 4 + 50*num_tris bytes
                import os
                file_size = os.path.getsize(stl_path)
                expected_binary = 84 + 50 * num_tris
                if abs(file_size - expected_binary) < 10:
                    # Binary STL
                    mn = [float("inf")] * 3
                    mx = [float("-inf")] * 3
                    for _ in range(num_tris):
                        data = f.read(50)
                        if len(data) < 50:
                            break
                        # Skip normal (12 bytes), read 3 vertices (36 bytes)
                        verts = struct.unpack("<12x9f2x", data)
                        for vi in range(3):
                            x, y, z = verts[vi * 3], verts[vi * 3 + 1], verts[vi * 3 + 2]
                            mn[0] = min(mn[0], x)
                            mn[1] = min(mn[1], y)
                            mn[2] = min(mn[2], z)
                            mx[0] = max(mx[0], x)
                            mx[1] = max(mx[1], y)
                            mx[2] = max(mx[2], z)
                    if mn[0] == float("inf"):
                        return None
                    return tuple(mn), tuple(mx)
                else:
                    # ASCII STL — re-read as text
                    f.seek(0)
                    text = f.read().decode("utf-8", errors="replace")
                    mn = [float("inf")] * 3
                    mx = [float("-inf")] * 3
                    import re
                    for m in re.finditer(r"vertex\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)", text):
                        x, y, z = float(m.group(1)), float(m.group(2)), float(m.group(3))
                        mn[0] = min(mn[0], x)
                        mn[1] = min(mn[1], y)
                        mn[2] = min(mn[2], z)
                        mx[0] = max(mx[0], x)
                        mx[1] = max(mx[1], y)
                        mx[2] = max(mx[2], z)
                    if mn[0] == float("inf"):
                        return None
                    return tuple(mn), tuple(mx)
        except Exception:
            return None

    result: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {}
    try:
        tree = ET.parse(urdf_path)
    except Exception:
        return result

    urdf_dir = os.path.dirname(os.path.abspath(urdf_path))

    for link_elem in tree.findall(".//link"):
        link_name = link_elem.get("name", "")
        if not link_name:
            continue

        # Try collision geometry first, fall back to visual
        for geom_tag in ("collision", "visual"):
            geom_elem = link_elem.find(geom_tag)
            if geom_elem is None:
                continue
            mesh_elem = geom_elem.find("geometry/mesh")
            if mesh_elem is None:
                continue
            filename = mesh_elem.get("filename", "")
            if not filename:
                continue

            # Resolve path
            if not os.path.isabs(filename):
                filename = os.path.join(urdf_dir, filename)

            # Parse scale
            scale_str = mesh_elem.get("scale", "1 1 1")
            scale = [float(s) for s in scale_str.split()]
            if len(scale) == 1:
                scale = scale * 3

            # Parse origin
            origin_elem = geom_elem.find("origin")
            ox = oy = oz = 0.0
            if origin_elem is not None:
                xyz_str = origin_elem.get("xyz", "0 0 0")
                ox, oy, oz = [float(v) for v in xyz_str.split()]

            extents = _read_stl_extents(filename)
            if extents is None:
                continue

            mn, mx = extents
            # Apply scale
            smn = [mn[i] * scale[i] for i in range(3)]
            smx = [mx[i] * scale[i] for i in range(3)]
            # Center and half-extents
            cx = (smn[0] + smx[0]) / 2.0 + ox
            cy = (smn[1] + smx[1]) / 2.0 + oy
            cz = (smn[2] + smx[2]) / 2.0 + oz
            hx = (smx[0] - smn[0]) / 2.0
            hy = (smx[1] - smn[1]) / 2.0
            hz = (smx[2] - smn[2]) / 2.0

            result[link_name] = ((cx, cy, cz), (hx, hy, hz))
            break  # Got geometry, no need to check visual fallback

    return result


class IsaacRuntime:
    """Command runtime used by the bridge server."""

    def __init__(self, *, headless: bool = True, environment: str = "") -> None:
        self._headless = headless
        self._environment = environment
        self._sessions: dict[str, SimulationSession] = {}
        self._batch_threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._engine = _IsaacWorldEngine(headless=headless)
        self._environment_loaded = False

    def load_environment_direct(self) -> None:
        """Load the configured environment directly (call from main thread only)."""
        if self._environment and not self._environment_loaded:
            self._load_environment_impl(self._environment)

    def _load_environment(self, environment: str) -> None:
        """Load environment via dispatcher (from worker thread)."""
        if not self._engine.available:
            logger.warning("[runtime] Cannot load environment — Isaac not available")
            return
        main_thread_dispatcher.submit(self._load_environment_impl, environment)
        self._environment_loaded = True

    def _load_environment_impl(self, environment: str) -> None:
        """Load a USD environment file into the scene (must run on main thread)."""
        try:
            import omni.usd  # type: ignore[import-not-found]
            from omni.isaac.nucleus import get_assets_root_path  # type: ignore[import-not-found]

            assets_root = get_assets_root_path()
            if assets_root is None:
                logger.warning("[runtime] Could not resolve Isaac assets root path")
                return

            env_path = f"{assets_root}/Isaac/Environments/Simple_Warehouse/{environment}"
            logger.info("[runtime] Loading environment: %s", env_path)

            self._engine.setup_scene()

            stage = omni.usd.get_context().get_stage()
            env_prim_path = "/World/Environment"
            if not stage.GetPrimAtPath(env_prim_path).IsValid():
                stage.DefinePrim(env_prim_path).GetReferences().AddReference(env_path)
                logger.info("[runtime] Environment loaded at %s", env_prim_path)
            else:
                logger.info("[runtime] Environment already loaded at %s", env_prim_path)
            self._environment_loaded = True
        except Exception as exc:
            logger.warning("[runtime] Failed to load environment '%s': %s", environment, exc)

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
                    "load_environment",
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

    def load_environment(
        self,
        usd_url: str,
        skip_ground_plane: bool = True,
    ) -> dict[str, Any]:
        """Load a USD environment into the scene.

        Must be called before ``teleop_start`` — the environment is
        referenced into the stage and persists across robot imports.
        """
        if not self._engine.available:
            raise IsaacRuntimeError(
                "ENGINE_UNAVAILABLE",
                "Isaac Sim engine not available",
            )

        # Ensure the scene is set up first (World, physics, etc.)
        self._engine.setup_scene()

        env_path = self._engine.load_environment(
            usd_url, skip_ground_plane=skip_ground_plane,
        )
        return {
            "env_prim_path": env_path,
            "usd_url": usd_url,
            "skip_ground_plane": skip_ground_plane,
        }

    def diagnose(self, *, prim_path: str = "/") -> dict[str, Any]:
        """Dump the USD prim tree under *prim_path*.

        Returns each prim's path, type name, and applied schemas, plus
        summary counts by type.  For joint prims, also returns body0/body1
        targets, drive stiffness/damping, and position targets.  For the
        articulation root, returns DOF count and joint names.

        Works without an articulation — just reads the stage.
        """
        if not self._engine.available:
            raise IsaacRuntimeError(
                "ISAAC_NOT_AVAILABLE",
                "Isaac Sim is not available — cannot inspect stage.",
            )

        def _do_diagnose() -> dict[str, Any]:
            def _to_json_safe(val: Any) -> Any:
                """Convert pxr types (Vec3f, Quatf, etc.) to JSON-safe Python types."""
                # Gf.Quatf / Quatd / Quath -> [real, i, j, k]
                type_name = type(val).__name__
                if "Quat" in type_name:
                    try:
                        return [float(val.GetReal()), float(val.GetImaginary()[0]),
                                float(val.GetImaginary()[1]), float(val.GetImaginary()[2])]
                    except Exception:
                        return str(val)
                # Gf.Vec* -> list of floats
                if "Vec" in type_name or "Matrix" in type_name:
                    try:
                        return [float(v) for v in val]
                    except (TypeError, ValueError):
                        return str(val)
                # Basic numeric types
                if isinstance(val, (int, float, bool, str)):
                    return val
                if isinstance(val, (list, tuple)):
                    return [_to_json_safe(v) for v in val]
                # Fallback
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return str(val)

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
            joint_details: list[dict[str, Any]] = []
            articulation_info: dict[str, Any] | None = None

            for prim in Usd.PrimRange(root):
                type_name = prim.GetTypeName() or ""
                schemas = [str(s) for s in prim.GetAppliedSchemas()]
                prim_info: dict[str, Any] = {
                    "path": str(prim.GetPath()),
                    "type": type_name,
                    "applied_schemas": schemas,
                }
                prims.append(prim_info)
                if type_name:
                    type_counts[type_name] = type_counts.get(type_name, 0) + 1

                # Extract joint details for physics joints
                if type_name in (
                    "PhysicsRevoluteJoint", "PhysicsPrismaticJoint",
                    "PhysicsFixedJoint", "PhysicsJoint",
                ):
                    jd: dict[str, Any] = {
                        "path": str(prim.GetPath()),
                        "type": type_name,
                    }
                    # body0/body1 relationship targets
                    for rel_name in ("physics:body0", "physics:body1"):
                        rel = prim.GetRelationship(rel_name)
                        if rel:
                            targets = rel.GetTargets()
                            jd[rel_name.replace(":", "_")] = (
                                [str(t) for t in targets] if targets else []
                            )
                    # Drive stiffness/damping/target from DriveAPI
                    for drive_ns in ("angular", "linear"):
                        stiffness_attr = prim.GetAttribute(
                            f"drive:{drive_ns}:physics:stiffness"
                        )
                        damping_attr = prim.GetAttribute(
                            f"drive:{drive_ns}:physics:damping"
                        )
                        target_pos_attr = prim.GetAttribute(
                            f"drive:{drive_ns}:physics:targetPosition"
                        )
                        target_vel_attr = prim.GetAttribute(
                            f"drive:{drive_ns}:physics:targetVelocity"
                        )
                        if stiffness_attr and stiffness_attr.HasValue():
                            jd[f"drive_{drive_ns}_stiffness"] = _to_json_safe(stiffness_attr.Get())
                        if damping_attr and damping_attr.HasValue():
                            jd[f"drive_{drive_ns}_damping"] = _to_json_safe(damping_attr.Get())
                        if target_pos_attr and target_pos_attr.HasValue():
                            jd[f"drive_{drive_ns}_target_position"] = _to_json_safe(target_pos_attr.Get())
                        if target_vel_attr and target_vel_attr.HasValue():
                            jd[f"drive_{drive_ns}_target_velocity"] = _to_json_safe(target_vel_attr.Get())
                    # Local position offsets
                    for pos_name in (
                        "physics:localPos0", "physics:localPos1",
                        "physics:localRot0", "physics:localRot1",
                    ):
                        attr = prim.GetAttribute(pos_name)
                        if attr and attr.HasValue():
                            val = attr.Get()
                            jd[pos_name.replace(":", "_")] = _to_json_safe(val)
                    joint_details.append(jd)

                # Extract articulation info
                if "PhysicsArticulationRootAPI" in schemas:
                    articulation_info = {
                        "prim_path": str(prim.GetPath()),
                        "schemas": schemas,
                    }

            # Try to get articulation DOF info from the world if available
            if articulation_info and self._engine.world is not None:
                try:
                    art_path = articulation_info["prim_path"]
                    # Check if there's an articulation in the scene
                    art = self._engine.world.scene.get_object(
                        art_path.split("/")[-1]
                    )
                    if art is not None and hasattr(art, "dof_names"):
                        articulation_info["dof_count"] = art.num_dof
                        articulation_info["dof_names"] = list(art.dof_names or [])
                        pos = art.get_joint_positions()
                        if pos is not None:
                            articulation_info["joint_positions_rad"] = [
                                float(p) for p in pos
                            ]
                except Exception as exc:
                    articulation_info["dof_error"] = str(exc)

            result: dict[str, Any] = {
                "prim_path": prim_path,
                "prim_count": len(prims),
                "prims": prims,
                "type_counts": dict(sorted(type_counts.items())),
                "joint_details": joint_details,
            }
            if articulation_info:
                result["articulation"] = articulation_info
            return result

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
        # Capture the old dispatcher BEFORE reload replaces it in the module dict.
        _old_dispatcher = _self_mod.main_thread_dispatcher
        importlib.reload(_self_mod)

        # Preserve the original main_thread_dispatcher so the bridge
        # server's pump loop (which holds a reference to the old module-
        # level singleton) continues to work with the reloaded module.
        _self_mod.main_thread_dispatcher = _old_dispatcher

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
        result: dict[str, Any] = {
            "prim_path": prim_path,
            "joint_count": joint_count,
            "link_count": link_count,
        }

        # Auto-capture verification screenshots (like FreeCAD's verify pattern)
        try:
            logger.info("[runtime] import_urdf: capturing verification views...")
            views = self._capture_verification_views()
            logger.info("[runtime] import_urdf: captured %d verification views", len(views))
            if views:
                result["verification_images"] = views
        except Exception as exc:
            logger.warning("[runtime] import_urdf: verification capture failed: %s", exc, exc_info=True)

        return result

    # ------------------------------------------------------------------
    # Simulation session lifecycle
    # ------------------------------------------------------------------

    def simulate_start(
        self,
        *,
        mechanism: dict[str, Any] | None = None,
        duration_s: float,
        dt_s: float,
        output_interval: float,
        profile: dict[str, Any] | None = None,
        urdf_path: str | None = None,
        import_config: dict[str, Any] | None = None,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Non-blocking. Setup scene + begin physics. Returns session_id.

        If *mechanism* is ``None`` and *urdf_path* is provided, a minimal
        mechanism is synthesized so the URDF can be imported and physics
        stepped without requiring the caller to construct a full mechanism.
        """
        logger.info(
            "[runtime] simulate_start: duration=%.3f dt=%.4f urdf=%s headless=%s",
            duration_s, dt_s, urdf_path, self._headless,
        )
        t0 = time.monotonic()

        # Synthesize a minimal mechanism when only a URDF is provided.
        if mechanism is None:
            if urdf_path is None:
                raise IsaacRuntimeError(
                    "INVALID_INPUT",
                    "Either mechanism or urdf_path must be provided",
                )
            mechanism = {
                "name": "urdf_physics_test",
                "parts": [{"id": "robot", "is_ground": False}],
                "joints": [],
                "drives": [],
            }

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
            # Clean up any previous scene so we start fresh.
            if self._engine._scene_ready:
                logger.info("[runtime] simulate_start: cleaning up previous scene")
                self._engine.cleanup()
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

        # Auto-capture verification screenshots after URDF import
        if verify and prim_path and self._engine.available:
            try:
                views = self._capture_verification_views()
                if views:
                    result["verification_images"] = views
            except Exception as exc:
                logger.warning("[runtime] simulate_start: verification capture failed: %s", exc)

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
                    if "joint_efforts" in sample:
                        entry["joint_efforts"] = sample["joint_efforts"]
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
        mechanism: dict[str, Any] | None = None,
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
        list(speeds.keys())

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

    def _configure_velocity_drives(
        self,
        articulation: Any,
        dof_index_map: dict[str, int],
    ) -> None:
        """Set mapped joints to velocity drive (stiffness=0, damping=1e4).

        Called when a controller declares ``drive_mode = "velocity"`` so
        that ``ArticulationAction(joint_velocities=...)`` actually moves
        the joints.  Position stiffness must be 0, otherwise the default
        position target (0) fights the velocity command.

        Uses ``articulation.set_gains()`` which sets gains directly on
        the live PhysX handles — more reliable than USD DriveAPI attr
        changes which may not be picked up after ``world.reset()``.
        """
        def _do() -> None:
            try:
                import numpy as np  # type: ignore[import-not-found]

                num_dof = articulation.num_dof
                # Read current gains as baseline
                try:
                    cur_kps = articulation.get_gains()[0]
                    cur_kds = articulation.get_gains()[1]
                    kps = np.array(cur_kps, dtype=np.float32)
                    kds = np.array(cur_kds, dtype=np.float32)
                except Exception:
                    kps = np.full(num_dof, 1000.0, dtype=np.float32)
                    kds = np.full(num_dof, 100.0, dtype=np.float32)

                configured = 0
                for _joint_name, idx in dof_index_map.items():
                    if 0 <= idx < num_dof:
                        kps[idx] = 0.0
                        kds[idx] = 1e4
                        configured += 1

                articulation.set_gains(kps, kds)

                logger.info(
                    "[runtime] _configure_velocity_drives: "
                    "stiffness=0 damping=1e4 on %d/%d DOFs via set_gains()",
                    configured, len(dof_index_map),
                )
                logger.info(
                    "[runtime] _configure_velocity_drives: kps=%s kds=%s",
                    kps.tolist(), kds.tolist(),
                )
            except Exception as exc:
                logger.warning("[runtime] _configure_velocity_drives failed: %s", exc)

        main_thread_dispatcher.submit(_do)

    def teleop_start(
        self,
        *,
        mechanism: dict[str, Any],
        profile: dict[str, Any] | None = None,
        urdf_path: str | None = None,
        import_config: dict[str, Any] | None = None,
        verify: bool = True,
        allow_partial: bool = False,
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

        # Parse and validate teleop config from profile
        try:
            teleop_config = TeleopConfig.from_profile(profile)
        except TeleopConfigError as exc:
            raise IsaacRuntimeError(
                "INVALID_INPUT",
                f"Invalid teleop profile: {exc.message}",
            ) from exc

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
                # Clean up any previous scene so we start fresh.
                if self._engine._scene_ready:
                    logger.info("[runtime] teleop_start: cleaning up previous scene")
                    self._engine.cleanup()

                cfg = URDFImportConfig.from_dict(import_config)
                prim_path, _jc, _lc, articulation, drive_warns = (
                    self._engine.start_simulation(
                        urdf_path, cfg, mech, skip_mechanism_drives=True,
                    )
                )
                warnings.extend(drive_warns)
            except IsaacRuntimeError:
                raise
            except Exception as exc:
                warnings.append(f"URDF import for teleop failed: {exc}")

        # Instantiate the controller via the registry.
        try:
            controller = create_controller(teleop_config)
        except ValueError as exc:
            raise IsaacRuntimeError(
                "INVALID_INPUT",
                str(exc),
            ) from exc

        # Resolve DOF name→index map and joint limits from articulation.
        # Fail fast if articulation is available but none of the required
        # joints could be mapped — this means the URDF doesn't match the
        # teleop config and the session would be useless.
        dof_index_map: dict[str, int] = {}
        joint_limits: dict[str, tuple[float, float]] = {}
        if articulation is not None:
            avail_dofs = _get_dof_names_safe(articulation)
            logger.info("[runtime] teleop_start: available DOFs (%d): %s",
                        len(avail_dofs), avail_dofs)
            # For multi-DOF controllers (2-DOF, 3-DOF), the controller
            # outputs targets keyed by leg_joint_names (e.g. 18 joints for
            # a 3-DOF hexapod).  The DOF map must use those names, not the
            # 1-DOF joint_names (which default to 6 hip names).
            if (teleop_config.dofs_per_leg >= 2
                    and teleop_config.leg_joint_names):
                joints_to_map = teleop_config.leg_joint_names
            else:
                joints_to_map = teleop_config.joint_names
            logger.info("[runtime] teleop_start: required joints (%d): %s",
                        len(joints_to_map), list(joints_to_map))
            dof_index_map, joint_limits = _resolve_dof_map(
                articulation, joints_to_map,
            )
            if not dof_index_map:
                raise IsaacRuntimeError(
                    "TELEOP_JOINT_MAP_FAILED",
                    "None of the required joint names could be mapped to "
                    "articulation DOFs",
                    details={
                        "required_joints": list(joints_to_map),
                        "available_dofs": _get_dof_names_safe(articulation),
                    },
                )
            missing = [
                j for j in teleop_config.joint_names
                if j not in dof_index_map
            ]
            if missing and not allow_partial:
                raise IsaacRuntimeError(
                    "TELEOP_JOINT_MAP_FAILED",
                    f"Partial joint map: {len(dof_index_map)}/{len(teleop_config.joint_names)} "
                    f"mapped, missing: {missing}. Pass allow_partial=True to proceed anyway.",
                    details={
                        "mapped": len(dof_index_map),
                        "required": len(teleop_config.joint_names),
                        "missing_joints": missing,
                        "available_dofs": avail_dofs,
                    },
                )
            elif missing:
                warnings.append(
                    f"Partial joint map: {len(dof_index_map)}/{len(teleop_config.joint_names)} "
                    f"mapped, missing: {missing}"
                )

        # If the controller uses velocity drive, reconfigure the mapped
        # joints: set stiffness=0 (disable position control) and keep
        # damping for velocity tracking.
        if (
            getattr(controller, "drive_mode", "position") == "velocity"
            and articulation is not None
            and self._engine.available
        ):
            self._configure_velocity_drives(articulation, dof_index_map)

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
            teleop_config=teleop_config,
            controller=controller,
            dof_index_map=dof_index_map,
            joint_limits=joint_limits,
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
            "profile_used": teleop_config.to_dict(),
            "controller_type": teleop_config.controller_type,
        }
        if prim_path:
            result["prim_path"] = prim_path
        if warnings:
            result["warnings"] = warnings

        # Auto-capture verification screenshots after URDF import
        if verify and prim_path and self._engine.available:
            try:
                views = self._capture_verification_views()
                if views:
                    result["verification_images"] = views
            except Exception as exc:
                logger.warning("[runtime] teleop_start: verification capture failed: %s", exc)

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
        result: dict[str, Any] = {"state": state, "uptime_s": uptime_s}
        # Append teleop telemetry (new keys — backward compatible)
        if session.teleop_config is not None:
            result["controller_type"] = session.teleop_config.controller_type
            result["joint_names"] = list(session.teleop_config.joint_names)
            result["last_joint_targets_rad"] = dict(session.last_joint_targets_rad)
            result["limit_clamp_count"] = session.limit_clamp_count
            result["tick_count"] = session.tick_count
            result["last_apply_ok"] = session.last_apply_ok
        return result

    def teleop_stop(self, *, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return {"stopped": True, "already_stopped": True}

        # Cleanup engine resources (World, imported prims) so a
        # subsequent teleop_start can start fresh.
        if session.prim_path:
            try:
                self._engine.cleanup()
            except Exception:
                pass

        result: dict[str, Any] = {"stopped": True}
        # Append final telemetry
        if session.teleop_config is not None:
            result["controller_type"] = session.teleop_config.controller_type
            result["tick_count"] = session.tick_count
            result["limit_clamp_count"] = session.limit_clamp_count
            result["last_joint_targets_rad"] = dict(session.last_joint_targets_rad)
        return result

    # ------------------------------------------------------------------
    # Main-thread teleop tick (called from pump loop — never from
    # background threads)
    # ------------------------------------------------------------------

    def tick_teleop(self, dt_s: float) -> bool:
        """Advance all active teleop sessions by one tick.

        Called from ``_pump_main_thread`` on the main thread before
        ``app.update()``.

        For each active teleop session:
        1. Compute joint targets via the session's controller.
        2. Clamp targets to joint limits.
        3. Apply targets to the articulation (if available).
        4. Step physics via ``world.step(render=True)``.
        5. Update session diagnostics.

        Returns True if at least one session was ticked (physics was
        stepped), so the pump loop can skip ``app.update()`` to avoid
        double-stepping.

        Thread-safety: reads ``session.state`` (written by background
        ``teleop_command`` threads under ``_lock``) but only writes to
        teleop-specific fields that the pump loop exclusively owns.
        """
        if dt_s <= 0:
            return False

        # Snapshot active teleop sessions under lock.
        with self._lock:
            teleop_sessions = [
                s for s in self._sessions.values()
                if s.session_type == "teleop"
                and s.teleop_config is not None
                and s.controller is not None
            ]

        if not teleop_sessions:
            return False

        for session in teleop_sessions:
            try:
                self._tick_one_session(session, dt_s)
            except Exception as exc:
                logger.warning(
                    "[runtime] tick_teleop: error on session %s: %s",
                    session.session_id, exc,
                )
                session.last_apply_ok = False

        return True

    def _tick_one_session(self, session: SimulationSession, dt_s: float) -> None:
        """Tick a single teleop session. Runs on the main thread."""
        config = session.teleop_config
        controller = session.controller
        assert config is not None and controller is not None

        # Read commanded state (written by teleop_command under lock).
        with self._lock:
            state_snapshot = TeleopState(
                vx_mps=session.state.vx_mps,
                yaw_rate_rps=session.state.yaw_rate_rps,
                body_height_m=session.state.body_height_m,
            )

        # Populate joint/body feedback from articulation (#6)
        if session.articulation is not None:
            try:
                self._populate_physics_state(session, state_snapshot)
            except Exception as exc:
                logger.debug(
                    "[runtime] _populate_physics_state failed: %s", exc,
                )

        # 1. Compute targets
        targets, new_phase = controller.compute_targets(
            state_snapshot, dt_s, config, session.gait_phase,
        )
        session.gait_phase = new_phase

        # 2. Clamp to joint limits
        clamped_targets, clamp_count = clamp_targets(targets, session.joint_limits)
        session.limit_clamp_count += clamp_count

        # 3. Record targets on session (for telemetry)
        session.last_joint_targets_rad = dict(clamped_targets)
        session.tick_count += 1

        # 4. Apply to articulation and step physics
        if session.articulation is not None and session.dof_index_map:
            try:
                self._apply_and_step(session, clamped_targets)
                session.last_apply_ok = True
            except Exception as exc:
                logger.warning(
                    "[runtime] _apply_and_step failed for %s: %s",
                    session.session_id, exc,
                )
                session.last_apply_ok = False
        else:
            # No articulation — still record targets for analytical mode
            session.last_apply_ok = True

        # 5. Sync filtered state back to session (for telemetry)
        session.filtered_vx = controller.filtered_vx
        session.filtered_yaw = controller.filtered_yaw
        session.filtered_height = controller.filtered_height

    def _populate_physics_state(
        self,
        session: SimulationSession,
        state: TeleopState,
    ) -> None:
        """Read joint positions/velocities and body state from the articulation.

        Populates ``state.joint_positions``, ``state.joint_velocities``,
        ``state.base_lin_vel``, ``state.base_ang_vel``, and
        ``state.projected_gravity`` so that RL policy controllers have
        real physics feedback instead of zeros.
        """
        import numpy as np  # type: ignore[import-not-found]

        art = session.articulation
        if art is None:
            return

        # Joint positions and velocities — reorder from DOF index order
        # (Isaac Sim internal) into config.joint_names order so that
        # controllers see obs[i] = joint_names[i] without remapping.
        try:
            joint_pos_raw = art.get_joint_positions()
            joint_vel_raw = art.get_joint_velocities()
            if joint_pos_raw is not None and joint_vel_raw is not None:
                raw_pos = joint_pos_raw.tolist() if hasattr(joint_pos_raw, 'tolist') else list(joint_pos_raw)
                raw_vel = joint_vel_raw.tolist() if hasattr(joint_vel_raw, 'tolist') else list(joint_vel_raw)

                cfg = session.teleop_config
                dof_map = session.dof_index_map
                if cfg is not None and dof_map:
                    # Reorder into joint_names order
                    ordered_pos: list[float] = []
                    ordered_vel: list[float] = []
                    for name in cfg.joint_names:
                        idx = dof_map.get(name)
                        if idx is not None and idx < len(raw_pos):
                            ordered_pos.append(raw_pos[idx])
                            ordered_vel.append(raw_vel[idx])
                        else:
                            # Unmapped joint — use default position (not 0.0)
                            ordered_pos.append(0.0)
                            ordered_vel.append(0.0)
                    n_mapped = sum(1 for n in cfg.joint_names if n in dof_map)
                    if n_mapped < len(cfg.joint_names) and session.tick_count <= 1:
                        logger.warning(
                            "Joint reorder: %d/%d joints mapped (unmapped get default pos)",
                            n_mapped, len(cfg.joint_names),
                        )
                    state.joint_positions = ordered_pos
                    state.joint_velocities = ordered_vel
                else:
                    # No config or dof_map — pass raw (best effort)
                    state.joint_positions = raw_pos
                    state.joint_velocities = raw_vel
        except Exception:
            pass

        # Root body state: position, orientation, velocities
        try:
            pos, quat = art.get_world_pose()  # pos (3,), quat (4,) wxyz
            vel = art.get_linear_velocity()    # (3,) world frame
            ang_vel = art.get_angular_velocity()  # (3,) world frame

            if pos is not None and quat is not None:
                # Convert to numpy for math
                if hasattr(pos, 'numpy'):
                    pos = pos.numpy() if hasattr(pos, 'numpy') else np.array(pos)
                if hasattr(quat, 'numpy'):
                    quat = quat.numpy() if hasattr(quat, 'numpy') else np.array(quat)
                if hasattr(vel, 'numpy'):
                    vel = vel.numpy() if hasattr(vel, 'numpy') else np.array(vel)
                if hasattr(ang_vel, 'numpy'):
                    ang_vel = ang_vel.numpy() if hasattr(ang_vel, 'numpy') else np.array(ang_vel)

                pos = np.asarray(pos, dtype=np.float64)
                quat = np.asarray(quat, dtype=np.float64)
                vel = np.asarray(vel, dtype=np.float64) if vel is not None else np.zeros(3)
                ang_vel = np.asarray(ang_vel, dtype=np.float64) if ang_vel is not None else np.zeros(3)

                # Rotate world-frame velocities to body frame using quaternion inverse
                # quat is wxyz
                w, x, y, z = float(quat[0]), float(quat[1]), float(quat[2]), float(quat[3])
                # Conjugate quaternion for inverse rotation
                # v_body = q* . v_world . q
                # Using the formula: v' = v + 2w(u x v) + 2(u x (u x v))
                # where u = [-x, -y, -z] (conjugate)
                u = np.array([-x, -y, -z])
                def _quat_rotate_inv(v: Any) -> Any:
                    uv = np.cross(u, v)
                    uuv = np.cross(u, uv)
                    return v + 2.0 * (w * uv + uuv)

                body_lin_vel = _quat_rotate_inv(vel)
                body_ang_vel = _quat_rotate_inv(ang_vel)

                # Projected gravity: rotate [0, 0, -1] to body frame (unit vector).
                # The controller scales this to match training convention.
                gravity_world = np.array([0.0, 0.0, -1.0])
                proj_gravity = _quat_rotate_inv(gravity_world)

                state.base_lin_vel = body_lin_vel.tolist()
                state.base_ang_vel = body_ang_vel.tolist()
                state.projected_gravity = proj_gravity.tolist()
        except Exception:
            pass

    def _apply_and_step(
        self,
        session: SimulationSession,
        targets: dict[str, float],
    ) -> None:
        """Apply joint targets to the articulation.

        Checks ``controller.drive_mode``: if ``"velocity"``, applies targets
        as joint velocities (rad/s) for continuous rotation (e.g. propellers).
        Otherwise applies as joint positions (rad) for pose control.

        Physics stepping and rendering are handled by ``app.update()`` in
        the main-thread pump loop — calling ``world.step()`` here would
        double-step physics per frame, causing jitter in non-headless mode.

        Must run on the main thread.
        """
        import numpy as np  # type: ignore[import-not-found]
        from omni.isaac.core.utils.types import ArticulationAction  # type: ignore[import-not-found]

        art = session.articulation
        num_dof = art.num_dof

        # Check if controller wants velocity drive
        use_velocity = getattr(session.controller, "drive_mode", "position") == "velocity"

        target_array = np.full(num_dof, float("nan"), dtype=np.float32)
        for joint_name, target_val in targets.items():
            idx = session.dof_index_map.get(joint_name)
            if idx is not None and 0 <= idx < num_dof:
                target_array[idx] = float(target_val)

        if use_velocity:
            art.apply_action(ArticulationAction(joint_velocities=target_array))
        else:
            art.apply_action(ArticulationAction(joint_positions=target_array))
        # Physics stepping is handled by app.update() in the pump loop.
        # Do NOT call world.step() here — it double-steps physics and
        # the second step may use stale targets causing jitter.

    def _capture_verification_views(
        self,
        width: int = 512,
        height: int = 512,
    ) -> list[dict[str, Any]]:
        """Capture 4 verification views of the scene (iso, front, top, right).

        Mirrors the FreeCAD pattern: low-res screenshots from multiple angles
        returned as part of the tool result for the LLM to inspect.
        """

        def _do_capture() -> list[dict[str, Any]]:
            import omni.usd  # type: ignore[import-not-found]
            from omni.kit.viewport.utility import (  # type: ignore[import-not-found]
                capture_viewport_to_file,
                get_active_viewport,
            )
            from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

            viewport = get_active_viewport()
            if viewport is None:
                return []

            # Compute scene bounding box from imported prims
            stage = omni.usd.get_context().get_stage()
            bbox_cache = UsdGeom.BBoxCache(0.0, [UsdGeom.Tokens.default_])
            center = Gf.Vec3d(0, 0, 0)
            radius = 0.5
            for prim_path in self._engine._imported_prims:
                prim = stage.GetPrimAtPath(prim_path)
                if prim.IsValid():
                    bbox = bbox_cache.ComputeWorldBound(prim)
                    box_range = bbox.ComputeAlignedRange()
                    if not box_range.IsEmpty():
                        mn = Gf.Vec3d(box_range.GetMin())
                        mx = Gf.Vec3d(box_range.GetMax())
                        center = (mn + mx) / 2.0
                        size = mx - mn
                        radius = max(size.GetLength() / 2.0, 0.01)

            dist = radius * 2.5
            # View definitions: (label, eye_offset_direction)
            view_defs = [
                ("iso",   Gf.Vec3d(0.577, 0.577, 0.577)),
                ("front", Gf.Vec3d(0.0, -1.0, 0.15)),
                ("top",   Gf.Vec3d(0.0, 0.0, 1.0)),
                ("right", Gf.Vec3d(1.0, 0.0, 0.15)),
            ]

            captured: list[dict[str, Any]] = []
            for label, direction in view_defs:
                eye = center + direction.GetNormalized() * dist
                _reposition_camera(
                    viewport,
                    [eye[0], eye[1], eye[2]],
                    [center[0], center[1], center[2]],
                    self._engine.sim_app,
                )
                try:
                    viewport.set_texture_resolution((width, height))
                except Exception:
                    pass

                # Pump frames for render
                app = self._engine.sim_app
                if app is not None:
                    for _ in range(8):
                        app.update()

                # Capture to file
                tmp_path = os.path.join(
                    tempfile.gettempdir(),
                    f"isaac_verify_{label}_{uuid.uuid4().hex[:8]}.png",
                )
                capture_viewport_to_file(viewport, tmp_path)
                # Wait for async capture
                for _ in range(120):
                    if app is not None:
                        app.update()
                    if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
                        break

                if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
                    with open(tmp_path, "rb") as f:
                        image_data = base64.b64encode(f.read()).decode("ascii")
                    captured.append({
                        "view": label,
                        "image_base64": image_data,
                        "mime_type": "image/png",
                        "width": width,
                        "height": height,
                    })
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            return captured

        try:
            return main_thread_dispatcher.submit(_do_capture)
        except Exception as exc:
            logger.warning("[runtime] verification capture failed: %s", exc)
            return []

    def _compute_auto_frame(
        self,
        distance_multiplier: float = 2.5,
        direction: tuple[float, float, float] | None = None,
    ) -> tuple[list[float], list[float]]:
        """Compute camera eye/target to frame imported prims.

        *direction* overrides the default isometric look-from direction.
        Returns (eye, target) as [x,y,z] lists.  Falls back to a
        sensible close-up default if bbox computation fails.
        """
        fallback_eye = [0.5, -0.5, 0.4]
        fallback_target = [0.0, 0.0, 0.15]
        try:
            import omni.usd  # type: ignore[import-not-found]
            from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

            stage = omni.usd.get_context().get_stage()
            bbox_cache = UsdGeom.BBoxCache(0.0, [UsdGeom.Tokens.default_])

            combined_min = None
            combined_max = None
            for prim_path in self._engine._imported_prims:
                prim = stage.GetPrimAtPath(prim_path)
                if not prim.IsValid():
                    continue
                bbox = bbox_cache.ComputeWorldBound(prim)
                box_range = bbox.ComputeAlignedRange()
                if box_range.IsEmpty():
                    continue
                mn = Gf.Vec3d(box_range.GetMin())
                mx = Gf.Vec3d(box_range.GetMax())
                if combined_min is None:
                    combined_min = mn
                    combined_max = mx
                else:
                    combined_min = Gf.Vec3d(
                        min(combined_min[0], mn[0]),
                        min(combined_min[1], mn[1]),
                        min(combined_min[2], mn[2]),
                    )
                    combined_max = Gf.Vec3d(
                        max(combined_max[0], mx[0]),
                        max(combined_max[1], mx[1]),
                        max(combined_max[2], mx[2]),
                    )

            if combined_min is None or combined_max is None:
                return fallback_eye, fallback_target

            center = (combined_min + combined_max) / 2.0
            size = combined_max - combined_min
            radius = max(size.GetLength() / 2.0, 0.01)
            dist = radius * distance_multiplier

            if direction is not None:
                dir_vec = Gf.Vec3d(*direction).GetNormalized()
            else:
                # Default isometric: front-right, slightly above
                dir_vec = Gf.Vec3d(0.577, -0.577, 0.577).GetNormalized()
            eye = center + dir_vec * dist
            target = center

            logger.debug(
                "[runtime] auto-frame: center=%s radius=%.3f dist=%.3f",
                center, radius, dist,
            )
            return (
                [eye[0], eye[1], eye[2]],
                [target[0], target[1], target[2]],
            )
        except Exception as exc:
            logger.debug("[runtime] auto-frame failed, using fallback: %s", exc)
            return fallback_eye, fallback_target

    # Preset direction table for camera positioning.
    _PRESET_DIRECTIONS: dict[str, tuple[float, float, float]] = {
        "iso":    (0.577, 0.577, 0.577),
        "front":  (0.0, -1.0, 0.15),
        "back":   (0.0, 1.0, 0.15),
        "top":    (0.0, 0.0, 1.0),
        "bottom": (0.0, 0.0, -1.0),
        "right":  (1.0, 0.0, 0.15),
        "left":   (-1.0, 0.0, 0.15),
    }

    def screenshot(
        self,
        *,
        width: int = 1280,
        height: int = 720,
        camera_position: list[float] | None = None,
        camera_target: list[float] | None = None,
        preset: str | None = None,
    ) -> dict[str, Any]:
        """Capture the Isaac Sim viewport as a base64-encoded PNG.

        If *camera_position* / *camera_target* are provided, the active
        viewport camera is repositioned before capture.  If *preset* is
        set (e.g. ``"front"``, ``"top"``) and no explicit camera coords
        are given, the camera is auto-framed from that direction.  All
        viewport operations are dispatched to the main thread.
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

            # Reposition camera: explicit coords > preset > auto-frame.
            if camera_position is not None or camera_target is not None:
                _reposition_camera(
                    viewport,
                    camera_position or [0.5, -0.5, 0.4],
                    camera_target or [0.0, 0.0, 0.0],
                    self._engine.sim_app,
                )
            elif preset and preset in self._PRESET_DIRECTIONS:
                direction = self._PRESET_DIRECTIONS[preset]
                auto_eye, auto_target = self._compute_auto_frame(direction=direction)
                _reposition_camera(
                    viewport,
                    auto_eye,
                    auto_target,
                    self._engine.sim_app,
                )
            else:
                # Auto-frame: compute scene bbox from imported prims and
                # place the camera at 2.5x bounding-sphere radius.
                auto_eye, auto_target = self._compute_auto_frame()
                _reposition_camera(
                    viewport,
                    auto_eye,
                    auto_target,
                    self._engine.sim_app,
                )

            # Resize viewport
            try:
                viewport.set_texture_resolution((width, height))
            except Exception as exc:
                logger.warning("[runtime] screenshot: set_texture_resolution failed: %s", exc)

            # Pump frames so camera change + viewport render take effect
            try:
                app = self._engine.sim_app
                if app is not None:
                    for _ in range(16):
                        app.update()
            except Exception:
                pass

            # Capture to temp file, read back, base64 encode
            tmp_path = os.path.join(tempfile.gettempdir(), f"isaac_screenshot_{uuid.uuid4().hex[:8]}.png")
            try:
                capture_viewport_to_file(viewport, tmp_path)

                # The capture is async — pump frames until the file appears.
                # Each app.update() advances the render pipeline; no sleep needed.
                app = self._engine.sim_app
                if app is not None:
                    for i in range(120):
                        app.update()
                        if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
                            logger.debug("[runtime] screenshot: file ready after %d frames", i + 1)
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


def _resolve_dof_map(
    articulation: Any,
    joint_names: tuple[str, ...],
) -> tuple[dict[str, int], dict[str, tuple[float, float]]]:
    """Resolve joint names to DOF indices and extract limits from an articulation.

    Uses the articulation's ``dof_names`` property to build the index map.
    Joint limits come from ``dof_properties`` if available.

    Returns (dof_index_map, joint_limits).  Missing joints are silently
    skipped — the caller decides whether incomplete mapping is fatal.
    """
    dof_index_map: dict[str, int] = {}
    joint_limits: dict[str, tuple[float, float]] = {}

    try:
        dof_names = articulation.dof_names
        if dof_names is None:
            return dof_index_map, joint_limits

        # Build name→index lookup.  DOF names may be full paths or short
        # names; try exact match first, then suffix match.
        name_to_idx: dict[str, int] = {}
        for idx, full_name in enumerate(dof_names):
            name_str = str(full_name)
            name_to_idx[name_str] = idx
            # Also index by the last path component (e.g. "hip_lf" from
            # "/World/robot/hip_lf").
            short = name_str.rsplit("/", 1)[-1]
            if short not in name_to_idx:
                name_to_idx[short] = idx

        for jname in joint_names:
            idx = name_to_idx.get(jname)
            if idx is not None:
                dof_index_map[jname] = idx

        # Extract joint limits from dof_properties (numpy structured array).
        try:
            props = articulation.dof_properties
            if props is not None:
                for jname, idx in dof_index_map.items():
                    lo = float(props["lower"][idx])
                    hi = float(props["upper"][idx])
                    if lo < hi:  # Only store if limits are meaningful
                        joint_limits[jname] = (lo, hi)
        except Exception:
            pass  # dof_properties not available or wrong shape

    except Exception as exc:
        logger.warning("[runtime] _resolve_dof_map failed (non-fatal): %s", exc)

    return dof_index_map, joint_limits


def _get_dof_names_safe(articulation: Any) -> list[str]:
    """Extract DOF names from an articulation, returning [] on failure."""
    try:
        names = articulation.dof_names
        if names is not None:
            return [str(n) for n in names]
    except Exception:
        pass
    return []


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
