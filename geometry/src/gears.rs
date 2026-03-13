/// Spur gear profile generation using involute curves.
///
/// Generates sketch elements (splines, arcs, lines) that can be passed
/// directly to `cad.sketch` for creating gear geometry in FreeCAD.

use std::f64::consts::PI;

use crate::involute::{
    half_tooth_angle, involute_curve_points, involute_function,
    mirror_y, rotate_point,
};
use crate::types::{GearParams, SketchElement, SketchResult};

/// Compute gear parameters from module, teeth count, and optional settings.
pub fn compute_gear_params(
    module: f64,
    teeth: u32,
    pressure_angle_deg: f64,
    clearance_coeff: f64,
    profile_shift: f64,
    backlash: f64,
) -> GearParams {
    let pa = pressure_angle_deg.to_radians();
    let z = teeth as f64;

    let pitch_d = module * z;
    let base_d = pitch_d * pa.cos();
    let tip_d = module * (z + 2.0 * (1.0 + profile_shift));
    let root_d = module * (z - 2.0 * (1.25 - profile_shift));

    GearParams {
        module,
        teeth,
        pressure_angle_deg,
        clearance_coeff,
        profile_shift,
        backlash,
        pitch_diameter: pitch_d,
        base_diameter: base_d,
        tip_diameter: tip_d,
        root_diameter: root_d,
    }
}

/// Compute internal (ring) gear parameters.
pub fn compute_internal_gear_params(
    module: f64,
    teeth: u32,
    pressure_angle_deg: f64,
    clearance_coeff: f64,
    profile_shift: f64,
    backlash: f64,
) -> GearParams {
    let pa = pressure_angle_deg.to_radians();
    let z = teeth as f64;

    let pitch_d = module * z;
    let base_d = pitch_d * pa.cos();
    // Internal gear: tip is smaller than pitch, root is larger
    let tip_d = module * (z - 2.0 * (1.0 + profile_shift));
    let root_d = module * (z + 2.0 * (1.25 - profile_shift));

    GearParams {
        module,
        teeth,
        pressure_angle_deg,
        clearance_coeff,
        profile_shift,
        backlash,
        pitch_diameter: pitch_d,
        base_diameter: base_d,
        tip_diameter: tip_d,
        root_diameter: root_d,
    }
}

/// Generate a full external spur gear profile as sketch elements.
///
/// The profile consists of:
/// - Involute spline curves for tooth flanks
/// - Arcs for the tip circle (connecting adjacent tooth flanks)
/// - Arcs for the root circle (connecting adjacent tooth roots)
///
/// The gear is centered at `center`.
pub fn spur_gear_profile(
    params: &GearParams,
    center: [f64; 2],
    num_involute_pts: usize,
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];
    let z = params.teeth;
    let rb = params.base_diameter / 2.0;
    let ra = params.tip_diameter / 2.0;
    let rf = params.root_diameter / 2.0;
    let pa = params.pressure_angle_deg.to_radians();

    // Effective start radius for involute (at or above base circle)
    let involute_start_r = rf.max(rb);

    // Angular offset: tooth thickness at pitch circle
    // Half-tooth angle at pitch circle + involute offset
    let rp = params.pitch_diameter / 2.0;
    let inv_pa = involute_function(pa);
    let tooth_half_angle = half_tooth_angle(z) + inv_pa - params.backlash / (2.0 * rp);

    let tooth_pitch = 2.0 * PI / z as f64;

    let mut elements = Vec::new();

    let needs_radial_lines = rf < rb;

    for i in 0..z {
        let tooth_angle = i as f64 * tooth_pitch;

        // Right flank involute (original)
        let inv_pts = involute_curve_points(rb, involute_start_r, ra, num_involute_pts);

        // The involute starts along the X axis. We need to rotate so the tooth
        // is centered at tooth_angle.
        // Right flank rotation: tooth_angle - tooth_half_angle
        let right_angle = tooth_angle - tooth_half_angle;
        let right_pts: Vec<[f64; 2]> = inv_pts
            .iter()
            .map(|p| {
                let (rx, ry) = rotate_point(p[0], p[1], right_angle);
                [rx + cx, ry + cy]
            })
            .collect();

        // Left flank: mirror the involute across the tooth center line, then rotate
        let left_angle = tooth_angle + tooth_half_angle;
        let left_pts: Vec<[f64; 2]> = inv_pts
            .iter()
            .map(|p| {
                let (mx, my) = mirror_y(p[0], p[1]);
                let (rx, ry) = rotate_point(mx, my, left_angle);
                [rx + cx, ry + cy]
            })
            .collect();

        // Right flank spline (root to tip)
        elements.push(SketchElement::Spline {
            points: right_pts.clone(),
            degree: 3,
            periodic: false,
            weights: None,
        });

        // Tip arc connecting right flank to left flank of same tooth
        let right_tip = right_pts.last().unwrap();
        let left_tip = left_pts.last().unwrap();
        let right_tip_angle = (right_tip[1] - cy).atan2(right_tip[0] - cx);
        let left_tip_angle = (left_tip[1] - cy).atan2(left_tip[0] - cx);

        elements.push(SketchElement::Arc {
            cx,
            cy,
            r: ra,
            start_angle: right_tip_angle.to_degrees(),
            end_angle: left_tip_angle.to_degrees(),
        });

        // Left flank spline (tip to root — reversed so it goes tip→root)
        let mut left_reversed = left_pts;
        left_reversed.reverse();
        elements.push(SketchElement::Spline {
            points: left_reversed.clone(),
            degree: 3,
            periodic: false,
            weights: None,
        });

        // Bridge from left involute base down to root circle (if needed)
        let left_base = left_reversed.last().unwrap();
        let left_base_angle = (left_base[1] - cy).atan2(left_base[0] - cx);

        if needs_radial_lines {
            let left_root_on_rf = [
                cx + rf * left_base_angle.cos(),
                cy + rf * left_base_angle.sin(),
            ];
            elements.push(SketchElement::Line {
                x1: left_base[0],
                y1: left_base[1],
                x2: left_root_on_rf[0],
                y2: left_root_on_rf[1],
            });
        }

        // Root arc connecting this tooth's left root to next tooth's right root
        let next_tooth_angle = (i + 1) as f64 * tooth_pitch;
        let next_right_angle = next_tooth_angle - tooth_half_angle;
        let next_inv_start = involute_curve_points(rb, involute_start_r, involute_start_r, 1);
        let (nrx, nry) = rotate_point(next_inv_start[0][0], next_inv_start[0][1], next_right_angle);
        let next_right_base_angle = (nry).atan2(nrx);

        let root_start = left_base_angle.to_degrees();
        let mut root_end = next_right_base_angle.to_degrees();
        if root_end < root_start {
            root_end += 360.0;
        }
        elements.push(SketchElement::Arc {
            cx,
            cy,
            r: rf,
            start_angle: root_start,
            end_angle: root_end,
        });

        // Bridge from root circle back up to next tooth's right involute base (if needed)
        if needs_radial_lines {
            let next_right_root_on_rf = [
                cx + rf * next_right_base_angle.cos(),
                cy + rf * next_right_base_angle.sin(),
            ];
            // Compute the actual involute start point at base radius
            let next_inv_base = involute_curve_points(rb, involute_start_r, involute_start_r, 1);
            let (next_bx, next_by) = rotate_point(next_inv_base[0][0], next_inv_base[0][1], next_right_angle);
            elements.push(SketchElement::Line {
                x1: next_right_root_on_rf[0],
                y1: next_right_root_on_rf[1],
                x2: cx + next_bx,
                y2: cy + next_by,
            });
        }
    }

    SketchResult {
        elements,
        metadata: params.to_metadata(),
    }
}

/// Generate a single tooth slot profile for use with pocket + polar pattern.
///
/// This creates the profile of one tooth gap (the space between two teeth)
/// that can be pocketed from a blank cylinder and then polar-patterned.
///
/// When the root circle is below the base circle (common for low tooth counts),
/// radial line segments bridge the gap between the involute endpoints and the
/// root arc, ensuring a fully closed wire suitable for FreeCAD pocket operations.
pub fn single_tooth_slot(
    params: &GearParams,
    center: [f64; 2],
    num_involute_pts: usize,
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];
    let rb = params.base_diameter / 2.0;
    let ra = params.tip_diameter / 2.0;
    let rf = params.root_diameter / 2.0;
    let pa = params.pressure_angle_deg.to_radians();

    let involute_start_r = rf.max(rb);
    let needs_radial_lines = rf < rb;

    let rp = params.pitch_diameter / 2.0;
    let inv_pa = involute_function(pa);
    let tooth_half_angle = half_tooth_angle(params.teeth) + inv_pa - params.backlash / (2.0 * rp);

    let tooth_pitch = 2.0 * PI / params.teeth as f64;

    // We generate the slot between tooth 0's left flank and tooth 1's right flank.
    let inv_pts = involute_curve_points(rb, involute_start_r, ra, num_involute_pts);

    // Left flank of tooth 0 (from tip to root)
    let left_angle = tooth_half_angle;
    let left_pts: Vec<[f64; 2]> = inv_pts
        .iter()
        .rev()
        .map(|p| {
            let (mx, my) = mirror_y(p[0], p[1]);
            let (rx, ry) = rotate_point(mx, my, left_angle);
            [rx + cx, ry + cy]
        })
        .collect();

    let right_angle_next = tooth_pitch - tooth_half_angle;
    let right_pts: Vec<[f64; 2]> = inv_pts
        .iter()
        .map(|p| {
            let (rx, ry) = rotate_point(p[0], p[1], right_angle_next);
            [rx + cx, ry + cy]
        })
        .collect();

    // Tip arc (closing the slot at the outside)
    let left_tip = &left_pts[0];
    let right_tip = right_pts.last().unwrap();
    let left_tip_angle = (left_tip[1] - cy).atan2(left_tip[0] - cx);
    let right_tip_angle = (right_tip[1] - cy).atan2(right_tip[0] - cx);

    let mut elements = Vec::new();

    // Left flank (tip to root)
    elements.push(SketchElement::Spline {
        points: left_pts.clone(),
        degree: 3,
        periodic: false,
        weights: None,
    });

    if needs_radial_lines {
        // Radial line from left involute base point down to root circle
        let left_base = left_pts.last().unwrap();
        let left_base_angle = (left_base[1] - cy).atan2(left_base[0] - cx);
        let left_root_on_rf = [
            cx + rf * left_base_angle.cos(),
            cy + rf * left_base_angle.sin(),
        ];
        elements.push(SketchElement::Line {
            x1: left_base[0],
            y1: left_base[1],
            x2: left_root_on_rf[0],
            y2: left_root_on_rf[1],
        });

        // Root arc from left root point to right root point
        let right_base = &right_pts[0];
        let right_base_angle = (right_base[1] - cy).atan2(right_base[0] - cx);
        let left_root_angle_deg = left_base_angle.to_degrees();
        let mut right_root_angle_deg = right_base_angle.to_degrees();
        if right_root_angle_deg < left_root_angle_deg {
            right_root_angle_deg += 360.0;
        }
        elements.push(SketchElement::Arc {
            cx,
            cy,
            r: rf,
            start_angle: left_root_angle_deg,
            end_angle: right_root_angle_deg,
        });

        // Radial line from root circle back up to right involute base point
        let right_root_on_rf = [
            cx + rf * right_base_angle.cos(),
            cy + rf * right_base_angle.sin(),
        ];
        elements.push(SketchElement::Line {
            x1: right_root_on_rf[0],
            y1: right_root_on_rf[1],
            x2: right_base[0],
            y2: right_base[1],
        });
    } else {
        // Root circle is at or above base circle — no gap, direct connection
        let left_root = left_pts.last().unwrap();
        let right_root = &right_pts[0];
        let left_root_angle = (left_root[1] - cy).atan2(left_root[0] - cx);
        let right_root_angle = (right_root[1] - cy).atan2(right_root[0] - cx);
        let mut end_deg = right_root_angle.to_degrees();
        if end_deg < left_root_angle.to_degrees() {
            end_deg += 360.0;
        }
        elements.push(SketchElement::Arc {
            cx,
            cy,
            r: rf,
            start_angle: left_root_angle.to_degrees(),
            end_angle: end_deg,
        });
    }

    // Right flank (root to tip)
    elements.push(SketchElement::Spline {
        points: right_pts,
        degree: 3,
        periodic: false,
        weights: None,
    });

    // Tip arc closing the slot
    elements.push(SketchElement::Arc {
        cx,
        cy,
        r: ra,
        start_angle: right_tip_angle.to_degrees(),
        end_angle: left_tip_angle.to_degrees(),
    });

    SketchResult {
        elements,
        metadata: params.to_metadata(),
    }
}

/// Generate a single internal tooth slot for use with pocket + polar pattern.
///
/// This creates the profile of one tooth gap on an internal (ring) gear.
/// The slot is the space between two adjacent internal teeth — i.e. one
/// period of the internal gear profile.  The closed wire consists of:
/// - Left flank involute (inner → outer)
/// - Root arc at rf (same tooth, outer, closing across the tooth land)
/// - Right flank involute reversed (outer → inner)
/// - Tip arc at ra (cross-tooth, inner, closing the gap)
///
/// When ra < rb, radial line segments bridge the involute endpoints to the
/// tip arc, ensuring a fully closed wire suitable for FreeCAD pocket.
pub fn single_internal_tooth_slot(
    params: &GearParams,
    center: [f64; 2],
    num_involute_pts: usize,
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];
    let z = params.teeth;
    let rb = params.base_diameter / 2.0;
    let ra = params.tip_diameter / 2.0; // inner (smaller) for internal gear
    let rf = params.root_diameter / 2.0; // outer (larger) for internal gear
    let pa = params.pressure_angle_deg.to_radians();

    let involute_start_r = ra.min(rf).max(rb);
    let involute_end_r = ra.max(rf);
    let needs_radial_lines = ra < rb;

    let rp = params.pitch_diameter / 2.0;
    let inv_pa = involute_function(pa);
    let tooth_half_angle = half_tooth_angle(z) + inv_pa - params.backlash / (2.0 * rp);

    let tooth_pitch = 2.0 * PI / z as f64;
    let slot_half_angle = tooth_pitch / 2.0 - tooth_half_angle;

    let inv_pts = involute_curve_points(rb, involute_start_r, involute_end_r, num_involute_pts);

    // We generate the slot between tooth 0's right flank and tooth 1's left flank.
    // (For internal gears, a "slot" is the gap between teeth on the inside.)

    // Right flank of tooth 0 (inner → outer)
    let right_angle = -slot_half_angle;
    let right_pts: Vec<[f64; 2]> = inv_pts
        .iter()
        .map(|p| {
            let (rx, ry) = rotate_point(p[0], p[1], right_angle);
            [rx + cx, ry + cy]
        })
        .collect();

    // Left flank of tooth 1 (inner → outer)
    let left_angle = tooth_pitch + slot_half_angle;
    let left_pts: Vec<[f64; 2]> = inv_pts
        .iter()
        .map(|p| {
            let (mx, my) = mirror_y(p[0], p[1]);
            let (rx, ry) = rotate_point(mx, my, left_angle);
            [rx + cx, ry + cy]
        })
        .collect();

    let mut elements = Vec::new();

    // 1. Right flank spline (inner → outer)
    elements.push(SketchElement::Spline {
        points: right_pts.clone(),
        degree: 3,
        periodic: false,
        weights: None,
    });

    // 2. Root arc (rf, outer): right_outer → left_outer (CCW across the tooth land)
    let right_outer = right_pts.last().unwrap();
    let left_outer = left_pts.last().unwrap();
    let right_outer_angle = (right_outer[1] - cy).atan2(right_outer[0] - cx);
    let left_outer_angle = (left_outer[1] - cy).atan2(left_outer[0] - cx);

    let root_start = right_outer_angle.to_degrees();
    let mut root_end = left_outer_angle.to_degrees();
    if root_end < root_start {
        root_end += 360.0;
    }
    elements.push(SketchElement::Arc {
        cx,
        cy,
        r: rf,
        start_angle: root_start,
        end_angle: root_end,
    });

    // 3. Left flank spline reversed (outer → inner)
    let mut left_reversed = left_pts;
    left_reversed.reverse();
    elements.push(SketchElement::Spline {
        points: left_reversed.clone(),
        degree: 3,
        periodic: false,
        weights: None,
    });

    // Bridge from left involute inner end to tip circle (if needed)
    let left_inner = left_reversed.last().unwrap();
    let left_inner_angle = (left_inner[1] - cy).atan2(left_inner[0] - cx);

    if needs_radial_lines {
        let left_tip_on_ra = [
            cx + ra * left_inner_angle.cos(),
            cy + ra * left_inner_angle.sin(),
        ];
        elements.push(SketchElement::Line {
            x1: left_inner[0],
            y1: left_inner[1],
            x2: left_tip_on_ra[0],
            y2: left_tip_on_ra[1],
        });
    }

    // 4. Tip arc (ra, inner): left_inner → right_inner (CCW, the long way around
    //    through the gap between teeth)
    let right_inner = &right_pts[0];
    let right_inner_angle = (right_inner[1] - cy).atan2(right_inner[0] - cx);

    // Go from right → left (CCW, the SHORT way through the gap)
    let tip_start = right_inner_angle.to_degrees();
    let mut tip_end = left_inner_angle.to_degrees();
    if tip_end < tip_start {
        tip_end += 360.0;
    }
    elements.push(SketchElement::Arc {
        cx,
        cy,
        r: ra,
        start_angle: tip_start,
        end_angle: tip_end,
    });

    // Bridge from tip circle back up to right involute inner end (if needed)
    if needs_radial_lines {
        let right_tip_on_ra = [
            cx + ra * right_inner_angle.cos(),
            cy + ra * right_inner_angle.sin(),
        ];
        elements.push(SketchElement::Line {
            x1: right_tip_on_ra[0],
            y1: right_tip_on_ra[1],
            x2: right_inner[0],
            y2: right_inner[1],
        });
    }

    SketchResult {
        elements,
        metadata: params.to_metadata(),
    }
}

/// Generate an internal (ring) gear profile.
///
/// For internal gears, the teeth point inward. The involute curves are
/// mirrored compared to external gears.
pub fn internal_gear_profile(
    params: &GearParams,
    center: [f64; 2],
    num_involute_pts: usize,
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];
    let z = params.teeth;
    let rb = params.base_diameter / 2.0;
    let ra = params.tip_diameter / 2.0; // For internal: smaller than pitch
    let rf = params.root_diameter / 2.0; // For internal: larger than pitch
    let pa = params.pressure_angle_deg.to_radians();

    let involute_start_r = ra.min(rf).max(rb);
    let involute_end_r = ra.max(rf);
    // For internal gears, tip is inner. If rb > ra, involute doesn't reach tip circle.
    let needs_radial_lines = ra < rb;

    let rp = params.pitch_diameter / 2.0;
    let inv_pa = involute_function(pa);
    let tooth_half_angle = half_tooth_angle(z) + inv_pa - params.backlash / (2.0 * rp);

    let tooth_pitch = 2.0 * PI / z as f64;

    // For internal gears, the tooth space is the raised part (pointing inward).
    // The profile is the inverse of external.
    let slot_half_angle = tooth_pitch / 2.0 - tooth_half_angle;

    let mut elements = Vec::new();

    for i in 0..z {
        let tooth_center = i as f64 * tooth_pitch;

        let inv_pts = involute_curve_points(rb, involute_start_r, involute_end_r, num_involute_pts);

        // Right flank (inner → outer, i.e. towards rf)
        let right_angle = tooth_center - slot_half_angle;
        let right_pts: Vec<[f64; 2]> = inv_pts
            .iter()
            .map(|p| {
                let (rx, ry) = rotate_point(p[0], p[1], right_angle);
                [rx + cx, ry + cy]
            })
            .collect();

        // Left flank (mirrored, inner → outer)
        let left_angle = tooth_center + slot_half_angle;
        let left_pts: Vec<[f64; 2]> = inv_pts
            .iter()
            .map(|p| {
                let (mx, my) = mirror_y(p[0], p[1]);
                let (rx, ry) = rotate_point(mx, my, left_angle);
                [rx + cx, ry + cy]
            })
            .collect();

        // For internal gears, the mirror flips angular ordering within a tooth:
        //   left_outer has SMALLER angle than right_outer
        //   right_inner has SMALLER angle than left_inner
        // So CCW arcs go: left_outer → right_outer (root, same tooth)
        //                  right_inner → next_left_inner (tip, cross-tooth)

        // 1. Left flank spline (inner → outer)
        elements.push(SketchElement::Spline {
            points: left_pts.clone(),
            degree: 3,
            periodic: false,
            weights: None,
        });

        // 2. Root arc (rf, same tooth): left_outer → right_outer (CCW, short way)
        let left_outer = left_pts.last().unwrap();
        let right_outer = right_pts.last().unwrap();
        let left_outer_angle = (left_outer[1] - cy).atan2(left_outer[0] - cx);
        let right_outer_angle = (right_outer[1] - cy).atan2(right_outer[0] - cx);

        let root_start = left_outer_angle.to_degrees();
        let mut root_end = right_outer_angle.to_degrees();
        if root_end < root_start {
            root_end += 360.0;
        }
        elements.push(SketchElement::Arc {
            cx,
            cy,
            r: rf,
            start_angle: root_start,
            end_angle: root_end,
        });

        // 3. Right flank spline reversed (outer → inner)
        let mut right_reversed = right_pts;
        right_reversed.reverse();
        elements.push(SketchElement::Spline {
            points: right_reversed.clone(),
            degree: 3,
            periodic: false,
            weights: None,
        });

        // Bridge from right involute inner end down to tip circle (if needed)
        let right_inner = right_reversed.last().unwrap();
        let right_inner_angle = (right_inner[1] - cy).atan2(right_inner[0] - cx);

        if needs_radial_lines {
            // 4. Radial line from right involute base (at rb) inward to tip circle (at ra)
            let right_tip_on_ra = [
                cx + ra * right_inner_angle.cos(),
                cy + ra * right_inner_angle.sin(),
            ];
            elements.push(SketchElement::Line {
                x1: right_inner[0],
                y1: right_inner[1],
                x2: right_tip_on_ra[0],
                y2: right_tip_on_ra[1],
            });
        }

        // 5. Tip arc (ra, cross-tooth): right_tip → next tooth's left_tip (CCW)
        let tip_start_angle = right_inner_angle;

        let next_tooth_center = (i + 1) as f64 * tooth_pitch;
        let next_left_angle = next_tooth_center + slot_half_angle;
        let next_inv_start = involute_curve_points(rb, involute_start_r, involute_start_r, 1);
        let (nmx, nmy) = mirror_y(next_inv_start[0][0], next_inv_start[0][1]);
        let (nlx, nly) = rotate_point(nmx, nmy, next_left_angle);
        let next_left_inner_angle = (nly).atan2(nlx);

        let tip_start = tip_start_angle.to_degrees();
        let mut tip_end = next_left_inner_angle.to_degrees();
        if tip_end < tip_start {
            tip_end += 360.0;
        }
        elements.push(SketchElement::Arc {
            cx,
            cy,
            r: ra,
            start_angle: tip_start,
            end_angle: tip_end,
        });

        // Bridge from tip circle back up to next tooth's left involute base (if needed)
        if needs_radial_lines {
            let next_left_tip_on_ra = [
                cx + ra * next_left_inner_angle.cos(),
                cy + ra * next_left_inner_angle.sin(),
            ];
            elements.push(SketchElement::Line {
                x1: next_left_tip_on_ra[0],
                y1: next_left_tip_on_ra[1],
                x2: cx + nlx,
                y2: cy + nly,
            });
        }
    }

    SketchResult {
        elements,
        metadata: params.to_metadata(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn default_params(teeth: u32) -> GearParams {
        compute_gear_params(1.0, teeth, 20.0, 0.25, 0.0, 0.0)
    }

    #[test]
    fn test_gear_params_pitch_diameter() {
        let p = compute_gear_params(2.0, 20, 20.0, 0.25, 0.0, 0.0);
        assert!((p.pitch_diameter - 40.0).abs() < 1e-10);
    }

    #[test]
    fn test_gear_params_base_diameter() {
        let p = compute_gear_params(2.0, 20, 20.0, 0.25, 0.0, 0.0);
        let expected = 40.0 * 20.0_f64.to_radians().cos();
        assert!((p.base_diameter - expected).abs() < 1e-10);
    }

    #[test]
    fn test_gear_params_tip_diameter() {
        let p = compute_gear_params(2.0, 20, 20.0, 0.25, 0.0, 0.0);
        // tip_d = m * (z + 2) for profile_shift = 0
        assert!((p.tip_diameter - 44.0).abs() < 1e-10);
    }

    #[test]
    fn test_gear_params_root_diameter() {
        let p = compute_gear_params(2.0, 20, 20.0, 0.25, 0.0, 0.0);
        // root_d = m * (z - 2.5) for profile_shift = 0
        assert!((p.root_diameter - 35.0).abs() < 1e-10);
    }

    #[test]
    fn test_gear_params_profile_shift() {
        let p = compute_gear_params(2.0, 20, 20.0, 0.25, 0.5, 0.0);
        // tip_d = m * (z + 2*(1+x)) = 2*(20+2*1.5) = 2*23 = 46
        assert!((p.tip_diameter - 46.0).abs() < 1e-10);
    }

    #[test]
    fn test_spur_gear_profile_element_count() {
        let p = default_params(12);
        let result = spur_gear_profile(&p, [0.0, 0.0], 20);
        // For 12T m=1: root_d=9.5, base_d=11.28 → rf < rb → needs radial lines
        // 6 elements per tooth: right spline, tip arc, left spline, line down, root arc, line up
        assert_eq!(result.elements.len(), 12 * 6);
    }

    #[test]
    fn test_spur_gear_profile_element_count_high_teeth() {
        // For z >= 42, root_d >= base_d, no radial lines needed → 4 per tooth
        let p = compute_gear_params(1.0, 50, 20.0, 0.25, 0.0, 0.0);
        let result = spur_gear_profile(&p, [0.0, 0.0], 20);
        assert_eq!(result.elements.len(), 50 * 4);
    }

    #[test]
    fn test_spur_gear_profile_spline_points() {
        let p = default_params(12);
        let result = spur_gear_profile(&p, [0.0, 0.0], 20);
        for elem in &result.elements {
            if let SketchElement::Spline { points, degree, .. } = elem {
                assert!(*degree >= 3);
                assert!(
                    points.len() >= (*degree as usize + 1),
                    "spline needs at least degree+1 points"
                );
            }
        }
    }

    #[test]
    fn test_spur_gear_profile_center_offset() {
        let p = default_params(12);
        let result = spur_gear_profile(&p, [10.0, 20.0], 20);
        // Arcs should be centered at the offset
        for elem in &result.elements {
            if let SketchElement::Arc { cx, cy, .. } = elem {
                assert!((cx - 10.0).abs() < 1e-10);
                assert!((cy - 20.0).abs() < 1e-10);
            }
        }
    }

    #[test]
    fn test_single_tooth_slot_element_count() {
        let p = default_params(18);
        let result = single_tooth_slot(&p, [0.0, 0.0], 20);
        // For 18T m=1: root_d=15.5, base_d=16.91 → rf < rb → needs radial lines
        // 6 elements: left spline, line down, root arc, line up, right spline, tip arc
        assert_eq!(result.elements.len(), 6);
    }

    #[test]
    fn test_single_tooth_slot_element_count_high_teeth() {
        // For z >= 42, no radial lines needed → 4 elements
        let p = compute_gear_params(1.0, 50, 20.0, 0.25, 0.0, 0.0);
        let result = single_tooth_slot(&p, [0.0, 0.0], 20);
        assert_eq!(result.elements.len(), 4);
    }

    /// Helper: get the endpoint of a sketch element (start or end).
    fn element_endpoint(elem: &SketchElement, cx: f64, cy: f64, start: bool) -> [f64; 2] {
        match elem {
            SketchElement::Spline { points, .. } => {
                if start { points[0] } else { *points.last().unwrap() }
            }
            SketchElement::Line { x1, y1, x2, y2 } => {
                if start { [*x1, *y1] } else { [*x2, *y2] }
            }
            SketchElement::Arc { cx: acx, cy: acy, r, start_angle, end_angle, .. } => {
                let a = if start { start_angle } else { end_angle };
                let rad = a.to_radians();
                [acx + r * rad.cos(), acy + r * rad.sin()]
            }
            SketchElement::Circle { .. } => [cx, cy],
        }
    }

    #[test]
    fn test_single_tooth_slot_closed_wire() {
        // Test that the tooth slot forms a closed wire for various tooth counts
        for teeth in [12, 16, 18, 20, 24, 30, 50] {
            let p = compute_gear_params(2.0, teeth, 20.0, 0.25, 0.0, 0.0);
            let result = single_tooth_slot(&p, [0.0, 0.0], 20);
            let n = result.elements.len();
            for i in 0..n {
                let end_of_i = element_endpoint(&result.elements[i], 0.0, 0.0, false);
                let start_of_next = element_endpoint(&result.elements[(i + 1) % n], 0.0, 0.0, true);
                let gap = ((end_of_i[0] - start_of_next[0]).powi(2)
                    + (end_of_i[1] - start_of_next[1]).powi(2))
                .sqrt();
                assert!(
                    gap < 1e-6,
                    "Gap of {:.6}mm between elements {} and {} for {}T gear (rf={:.3}, rb={:.3})",
                    gap, i, (i + 1) % n, teeth,
                    p.root_diameter / 2.0, p.base_diameter / 2.0,
                );
            }
        }
    }

    #[test]
    fn test_spur_gear_profile_closed_wire() {
        // Test that the full gear profile forms a closed wire
        for teeth in [12, 16, 20, 50] {
            let p = compute_gear_params(2.0, teeth, 20.0, 0.25, 0.0, 0.0);
            let result = spur_gear_profile(&p, [0.0, 0.0], 20);
            let n = result.elements.len();
            for i in 0..n {
                let end_of_i = element_endpoint(&result.elements[i], 0.0, 0.0, false);
                let start_of_next = element_endpoint(&result.elements[(i + 1) % n], 0.0, 0.0, true);
                let gap = ((end_of_i[0] - start_of_next[0]).powi(2)
                    + (end_of_i[1] - start_of_next[1]).powi(2))
                .sqrt();
                assert!(
                    gap < 1e-6,
                    "Gap of {:.6}mm between elements {} and {} for {}T gear",
                    gap, i, (i + 1) % n, teeth,
                );
            }
        }
    }

    #[test]
    fn test_internal_gear_params() {
        let p = compute_internal_gear_params(2.0, 40, 20.0, 0.25, 0.0, 0.0);
        // For internal gear, tip < pitch < root
        assert!(p.tip_diameter < p.pitch_diameter);
        assert!(p.root_diameter > p.pitch_diameter);
    }

    #[test]
    fn test_internal_gear_profile_element_count() {
        // z=30 m=1: ra=14.0, rb=14.095 → rb > ra → needs radial lines → 6 per tooth
        let p = compute_internal_gear_params(1.0, 30, 20.0, 0.25, 0.0, 0.0);
        let result = internal_gear_profile(&p, [0.0, 0.0], 20);
        assert_eq!(result.elements.len(), 30 * 6);
    }

    #[test]
    fn test_internal_gear_profile_element_count_high_teeth() {
        // z=40 m=1: ra=19.0, rb=18.79 → rb < ra → no radial lines → 4 per tooth
        let p = compute_internal_gear_params(1.0, 40, 20.0, 0.25, 0.0, 0.0);
        let result = internal_gear_profile(&p, [0.0, 0.0], 20);
        assert_eq!(result.elements.len(), 40 * 4);
    }

    #[test]
    fn test_spur_gear_arc_angles_no_wrap() {
        // 57-tooth gear triggers the ±180° wrapping bug on the last tooth
        let p = compute_gear_params(2.0, 57, 20.0, 0.25, 0.0, 0.0);
        let result = spur_gear_profile(&p, [0.0, 0.0], 20);
        for elem in &result.elements {
            if let SketchElement::Arc {
                start_angle,
                end_angle,
                ..
            } = elem
            {
                assert!(
                    end_angle >= start_angle,
                    "Arc end_angle ({}) < start_angle ({}): wrapping bug",
                    end_angle,
                    start_angle
                );
                assert!(
                    (end_angle - start_angle) < 180.0,
                    "Arc span {} deg is too large (likely wrapped incorrectly)",
                    end_angle - start_angle
                );
            }
        }
    }

    #[test]
    fn test_internal_gear_profile_closed_wire() {
        // Test that the internal gear profile forms a closed wire
        for teeth in [30, 40, 54, 57] {
            let p = compute_internal_gear_params(1.25, teeth, 20.0, 0.25, 0.0, 0.0);
            let result = internal_gear_profile(&p, [0.0, 0.0], 20);
            let n = result.elements.len();
            for i in 0..n {
                let end_of_i = element_endpoint(&result.elements[i], 0.0, 0.0, false);
                let start_of_next = element_endpoint(&result.elements[(i + 1) % n], 0.0, 0.0, true);
                let gap = ((end_of_i[0] - start_of_next[0]).powi(2)
                    + (end_of_i[1] - start_of_next[1]).powi(2))
                .sqrt();
                assert!(
                    gap < 1e-6,
                    "Gap of {:.6}mm between elements {} and {} for {}T internal gear",
                    gap, i, (i + 1) % n, teeth,
                );
            }
        }
    }

    #[test]
    fn test_internal_gear_arc_angles_no_wrap() {
        let p = compute_internal_gear_params(2.0, 57, 20.0, 0.25, 0.0, 0.0);
        let result = internal_gear_profile(&p, [0.0, 0.0], 20);
        for elem in &result.elements {
            if let SketchElement::Arc {
                start_angle,
                end_angle,
                ..
            } = elem
            {
                assert!(
                    end_angle >= start_angle,
                    "Arc end_angle ({}) < start_angle ({}): wrapping bug",
                    end_angle,
                    start_angle
                );
                assert!(
                    (end_angle - start_angle) < 180.0,
                    "Arc span {} deg is too large (likely wrapped incorrectly)",
                    end_angle - start_angle
                );
            }
        }
    }

    #[test]
    fn test_metadata_present() {
        let p = default_params(18);
        let result = spur_gear_profile(&p, [0.0, 0.0], 20);
        assert!(result.metadata.contains_key("pitch_diameter"));
        assert!(result.metadata.contains_key("base_diameter"));
        assert!(result.metadata.contains_key("tip_diameter"));
        assert!(result.metadata.contains_key("root_diameter"));
    }

    #[test]
    fn test_single_internal_tooth_slot_element_count() {
        // z=30 m=1: ra=14.0, rb=14.095 → rb > ra → needs radial lines → 6 elements
        let p = compute_internal_gear_params(1.0, 30, 20.0, 0.25, 0.0, 0.0);
        let result = single_internal_tooth_slot(&p, [0.0, 0.0], 20);
        assert_eq!(result.elements.len(), 6);
    }

    #[test]
    fn test_single_internal_tooth_slot_element_count_high_teeth() {
        // z=40 m=1: ra=19.0, rb=18.79 → rb < ra → no radial lines → 4 elements
        let p = compute_internal_gear_params(1.0, 40, 20.0, 0.25, 0.0, 0.0);
        let result = single_internal_tooth_slot(&p, [0.0, 0.0], 20);
        assert_eq!(result.elements.len(), 4);
    }

    #[test]
    fn test_single_internal_tooth_slot_closed_wire() {
        for teeth in [30, 36, 40, 54, 57] {
            let p = compute_internal_gear_params(1.25, teeth, 20.0, 0.25, 0.0, 0.0);
            let result = single_internal_tooth_slot(&p, [0.0, 0.0], 20);
            let n = result.elements.len();
            for i in 0..n {
                let end_of_i = element_endpoint(&result.elements[i], 0.0, 0.0, false);
                let start_of_next = element_endpoint(&result.elements[(i + 1) % n], 0.0, 0.0, true);
                let gap = ((end_of_i[0] - start_of_next[0]).powi(2)
                    + (end_of_i[1] - start_of_next[1]).powi(2))
                .sqrt();
                assert!(
                    gap < 1e-6,
                    "Gap of {:.6}mm between elements {} and {} for {}T internal tooth slot",
                    gap, i, (i + 1) % n, teeth,
                );
            }
        }
    }
}
