from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import cadquery as cq

from server.cad_gen import (
    HEAT_SET_BOSS_DIAMETERS,
    HEAT_SET_DIAMETERS,
    GenerateResult,
    normalize_step_timestamp,
    parse_interface,
    to_base64,
)


class BoxCadGenerator:
    """Envelope-box CAD generator for CNC and 3D-printing processes."""

    def supported_formats(self) -> tuple[str, ...]:
        return ("step", "stl", "freecad")

    def generate(
        self,
        spec: dict[str, Any],
        output_format: str,
        output_path: Path,
        options: dict[str, Any],
    ) -> GenerateResult:
        warnings: list[str] = []
        meta = spec.get("meta", {})
        part = spec.get("part", {})
        mfg = spec.get("manufacturing", {})
        process = meta.get("process", "cnc")

        env = part.get("envelope", {})
        env_x = float(env.get("x", 0))
        env_y = float(env.get("y", 0))
        env_z = float(env.get("z", 0))

        if env_x <= 0 or env_y <= 0 or env_z <= 0:
            raise ValueError(
                f"Invalid envelope dimensions: x={env_x}, y={env_y}, z={env_z}. "
                "All must be positive."
            )

        # P0: envelope box (fatal)
        solid = cq.Workplane("XY").box(env_x, env_y, env_z)
        feature_count = 1  # the box itself

        # P1: interface holes
        interfaces = part.get("interfaces", [])
        if isinstance(interfaces, list):
            for i, iface in enumerate(interfaces):
                if not isinstance(iface, str):
                    warnings.append(f"interfaces[{i}]: expected string, skipped")
                    continue
                try:
                    solid, count = self._add_holes(
                        solid, iface, i, env_x, env_y, env_z, warnings,
                    )
                    feature_count += count
                except Exception as e:
                    warnings.append(f"interfaces[{i}]: hole failed ({e}) — skipped")

        # P1: edge fillets
        fillet_radius = self._determine_fillet_radius(mfg, options, warnings, process)
        if fillet_radius > 0:
            try:
                solid = solid.edges().fillet(fillet_radius)
                feature_count += 1
            except Exception as e:
                warnings.append(f"fillet failed ({e}) — sharp edges retained")

        # Solid validation
        try:
            if not solid.val().isValid():
                warnings.append("OCCT solid validation failed — geometry may be degenerate")
        except Exception:
            warnings.append("Could not validate solid geometry")

        # P2: free-text warnings
        critical = part.get("critical_features", [])
        if isinstance(critical, list):
            for j, feat in enumerate(critical):
                if isinstance(feat, str) and feat.strip():
                    warnings.append(
                        f"critical_features[{j}]: '{feat}' — not modeled, review required"
                    )

        process_notes = mfg.get("process_notes", "")
        if isinstance(process_notes, str) and process_notes.strip():
            if "deburr" in process_notes.lower():
                warnings.append(
                    f"process_notes: '{process_notes}' — applied {fillet_radius}mm edge fillet"
                )

        if process == "print_3d":
            self._emit_print_3d_warnings(mfg, warnings)

        # Export
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if output_format == "step":
            cq.exporters.export(solid, str(output_path), exportType="STEP")
            ts = meta.get("created_at", "2000-01-01T00:00:00")
            normalize_step_timestamp(output_path, ts)
        elif output_format == "stl":
            stl_tol = float(options.get("stl_tolerance", 0.1))
            angular_tol = 0.1  # radians, fixed
            cq.exporters.export(
                solid,
                str(output_path),
                exportType="STL",
                tolerance=stl_tol,
                angularTolerance=angular_tol,
            )
        elif output_format == "freecad":
            self._export_freecad(solid, output_path, meta)

        file_size = output_path.stat().st_size
        max_inline = int(options.get("max_inline_bytes", 65536))
        cad_data = to_base64(output_path) if file_size <= max_inline else None

        metadata = {
            "format": output_format,
            "units": meta.get("units", "mm"),
            "feature_count": feature_count,
            "file_size_bytes": file_size,
            "generated_at": meta.get("created_at", ""),
            "spec_hash": "",  # filled by caller in tools.py
        }

        return GenerateResult(
            file_path=output_path,
            cad_data=cad_data,
            metadata=metadata,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # FreeCAD (.FCStd) export
    # ------------------------------------------------------------------

    @staticmethod
    def _export_freecad(
        solid: cq.Workplane,
        output_path: Path,
        meta: dict[str, Any],
    ) -> None:
        """Export geometry as a FreeCAD .FCStd file via a temporary STEP intermediate."""
        import tempfile

        # Write STEP to a temp file, then import into FreeCAD and save as .FCStd.
        tmp_step = Path(tempfile.mktemp(suffix=".step"))
        try:
            cq.exporters.export(solid, str(tmp_step), exportType="STEP")

            try:
                import FreeCAD  # type: ignore
            except ImportError:
                import sys

                candidate = Path("/usr/lib/freecad-python3/lib")
                if (candidate / "FreeCAD.so").exists() and str(candidate) not in sys.path:
                    sys.path.insert(0, str(candidate))
                    import FreeCAD  # type: ignore  # noqa: F811
                else:
                    raise ImportError(
                        "FreeCAD Python modules not found. "
                        "Install FreeCAD or set PYTHONPATH to include FreeCAD's lib directory."
                    )

            try:
                import Import as FreeCADImport  # type: ignore
            except ImportError:
                FreeCADImport = None  # type: ignore

            doc = FreeCAD.newDocument("SpecModel")
            part_name = meta.get("part_name", "PartFromSpec")
            if not isinstance(part_name, str) or not part_name.strip():
                part_name = "PartFromSpec"
            doc.Label = part_name

            if FreeCADImport is not None:
                FreeCADImport.insert(str(tmp_step), doc.Name)
            else:
                raise ImportError(
                    "FreeCAD Import module not available; cannot import STEP geometry."
                )

            doc.recompute()
            doc.saveAs(str(output_path))
            FreeCAD.closeDocument(doc.Name)
        finally:
            if tmp_step.exists():
                tmp_step.unlink()

    # ------------------------------------------------------------------
    # Hole placement
    # ------------------------------------------------------------------

    def _add_holes(
        self,
        solid: cq.Workplane,
        iface: str,
        idx: int,
        env_x: float,
        env_y: float,
        env_z: float,
        warnings: list[str],
    ) -> tuple[cq.Workplane, int]:
        """Parse interface string and add holes. Returns (solid, hole_count)."""
        parsed = parse_interface(iface)
        if parsed is None:
            warnings.append(f"interfaces[{idx}]: could not parse — skipped")
            return solid, 0

        dia = parsed.diameter_mm
        depth = env_z  # through-hole by default

        if parsed.pattern_x is not None and parsed.pattern_y is not None:
            positions = self._rectangular_grid(
                parsed.count, parsed.pattern_x, parsed.pattern_y,
            )
        else:
            positions = self._linear_pattern(
                parsed.count, env_x, env_y,
            )

        warnings.append(
            f"interfaces[{idx}]: hole positions are heuristic — verify spacing"
        )

        if parsed.hole_type == "heat-set":
            for px, py in positions:
                solid, _ = self._add_insert_boss(
                    solid, px, py, parsed.thread, dia, env_z,
                )
        else:
            for px, py in positions:
                solid = (
                    solid
                    .faces(">Z")
                    .workplane()
                    .center(px, py)
                    .hole(dia, depth)
                )

        return solid, len(positions)

    @staticmethod
    def _rectangular_grid(
        count: int,
        pattern_x: float,
        pattern_y: float,
    ) -> list[tuple[float, float]]:
        """Place holes on a rectangular grid centered at origin."""
        if count == 1:
            return [(0.0, 0.0)]
        if count == 2:
            # Pair along X
            hx = pattern_x / 2
            return [(-hx, 0.0), (hx, 0.0)]
        if count == 4:
            # Four corners
            hx = pattern_x / 2
            hy = pattern_y / 2
            return [(-hx, -hy), (hx, -hy), (-hx, hy), (hx, hy)]
        # General: fill rows
        cols = int(math.ceil(math.sqrt(count)))
        rows = int(math.ceil(count / cols))
        hx = pattern_x / 2 if cols > 1 else 0.0
        hy = pattern_y / 2 if rows > 1 else 0.0
        positions: list[tuple[float, float]] = []
        placed = 0
        for r in range(rows):
            for c in range(cols):
                if placed >= count:
                    break
                x = -hx + (pattern_x * c / max(cols - 1, 1)) if cols > 1 else 0.0
                y = -hy + (pattern_y * r / max(rows - 1, 1)) if rows > 1 else 0.0
                positions.append((x, y))
                placed += 1
        return positions

    @staticmethod
    def _linear_pattern(
        count: int,
        env_x: float,
        env_y: float,
    ) -> list[tuple[float, float]]:
        """Place holes linearly along the longest axis, 60% spacing, centered."""
        if count == 1:
            return [(0.0, 0.0)]

        along_x = env_x >= env_y
        span = (env_x if along_x else env_y) * 0.6
        step = span / (count - 1) if count > 1 else 0.0
        start = -span / 2

        positions: list[tuple[float, float]] = []
        for i in range(count):
            offset = start + step * i
            if along_x:
                positions.append((offset, 0.0))
            else:
                positions.append((0.0, offset))
        return positions

    # ------------------------------------------------------------------
    # Insert boss (heat-set inserts for 3D printing)
    # ------------------------------------------------------------------

    @staticmethod
    def _add_insert_boss(
        solid: cq.Workplane,
        cx: float,
        cy: float,
        thread: str,
        insert_dia: float,
        env_z: float,
    ) -> tuple[cq.Workplane, int]:
        """Add a raised cylindrical boss with insert hole on the top face."""
        boss_od = HEAT_SET_BOSS_DIAMETERS.get(thread)
        if boss_od is None:
            boss_od = insert_dia * 1.8
        boss_r = boss_od / 2.0
        boss_height = min(env_z * 0.6, 8.0)

        # Extrude boss on top face
        boss = (
            cq.Workplane("XY")
            .transformed(offset=(cx, cy, env_z / 2.0))
            .circle(boss_r)
            .extrude(boss_height)
        )
        solid = solid.union(boss)

        # Drill insert hole into the boss from above
        solid = (
            solid
            .faces(">Z")
            .workplane()
            .center(cx, cy)
            .hole(insert_dia, boss_height)
        )
        return solid, 1

    # ------------------------------------------------------------------
    # Fillet radius selection
    # ------------------------------------------------------------------

    @staticmethod
    def _determine_fillet_radius(
        mfg: dict[str, Any],
        options: dict[str, Any],
        warnings: list[str],
        process: str = "cnc",
    ) -> float:
        """Choose fillet radius from options, process notes, or default."""
        if "fillet_radius" in options:
            return float(options["fillet_radius"])

        # 3D-printed parts don't need deburring — no fillets by default
        if process == "print_3d":
            return 0.0

        process_notes = mfg.get("process_notes", "")
        if isinstance(process_notes, str) and "deburr" in process_notes.lower():
            return 0.3

        warnings.append(
            "No explicit fillet spec; applied 0.2mm default break-edge. "
            "Verify edge treatment requirements."
        )
        return 0.2

    # ------------------------------------------------------------------
    # 3D-printing warnings
    # ------------------------------------------------------------------

    @staticmethod
    def _emit_print_3d_warnings(
        mfg: dict[str, Any],
        warnings: list[str],
    ) -> None:
        """Warn about print_3d fields not modeled in geometry."""
        appearance = mfg.get("appearance", {})
        if isinstance(appearance, dict):
            color = appearance.get("color")
            if color:
                warnings.append(
                    f"appearance.color: '{color}' — not modeled in geometry"
                )
            finish = appearance.get("finish")
            if finish:
                warnings.append(
                    f"appearance.finish: '{finish}' — not modeled in geometry"
                )

        post = mfg.get("post_processing", [])
        if isinstance(post, list) and post:
            warnings.append(
                f"post_processing: {post} — not modeled in geometry"
            )

        in_house = mfg.get("in_house_settings", {})
        if isinstance(in_house, dict) and in_house:
            warnings.append(
                "in_house_settings: print parameters not modeled in geometry"
            )
