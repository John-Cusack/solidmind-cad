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

        // Root arc connecting this tooth's left root to next tooth's right root
        let left_root = left_reversed.last().unwrap();
        let left_root_angle = (left_root[1] - cy).atan2(left_root[0] - cx);

        let next_tooth_angle = (i + 1) as f64 * tooth_pitch;
        let next_right_angle = next_tooth_angle - tooth_half_angle;
        let next_inv_start = involute_curve_points(rb, involute_start_r, involute_start_r, 1);
        let (nrx, nry) = rotate_point(next_inv_start[0][0], next_inv_start[0][1], next_right_angle);
        let next_right_root_angle = (nry).atan2(nrx);

        let root_start = left_root_angle.to_degrees();
        let mut root_end = next_right_root_angle.to_degrees();
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

    // Root arc
    let left_root = left_pts.last().unwrap();
    let left_root_angle = (left_root[1] - cy).atan2(left_root[0] - cx);

    let right_angle_next = tooth_pitch - tooth_half_angle;
    let right_pts: Vec<[f64; 2]> = inv_pts
        .iter()
        .map(|p| {
            let (rx, ry) = rotate_point(p[0], p[1], right_angle_next);
            [rx + cx, ry + cy]
        })
        .collect();

    let right_root = &right_pts[0];
    let right_root_angle = (right_root[1] - cy).atan2(right_root[0] - cx);

    // Tip arc (closing the slot at the outside)
    let left_tip = &left_pts[0];
    let right_tip = right_pts.last().unwrap();
    let left_tip_angle = (left_tip[1] - cy).atan2(left_tip[0] - cx);
    let right_tip_angle = (right_tip[1] - cy).atan2(right_tip[0] - cx);

    let mut elements = Vec::new();

    // Left flank (tip to root)
    elements.push(SketchElement::Spline {
        points: left_pts,
        degree: 3,
        periodic: false,
        weights: None,
    });

    // Root arc
    elements.push(SketchElement::Arc {
        cx,
        cy,
        r: rf,
        start_angle: left_root_angle.to_degrees(),
        end_angle: right_root_angle.to_degrees(),
    });

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

        // Right flank
        let right_angle = tooth_center - slot_half_angle;
        let right_pts: Vec<[f64; 2]> = inv_pts
            .iter()
            .map(|p| {
                let (rx, ry) = rotate_point(p[0], p[1], right_angle);
                [rx + cx, ry + cy]
            })
            .collect();

        // Left flank (mirrored)
        let left_angle = tooth_center + slot_half_angle;
        let left_pts: Vec<[f64; 2]> = inv_pts
            .iter()
            .map(|p| {
                let (mx, my) = mirror_y(p[0], p[1]);
                let (rx, ry) = rotate_point(mx, my, left_angle);
                [rx + cx, ry + cy]
            })
            .collect();

        // Tip arc (inner, smaller radius) — from right to left
        let right_inner = &right_pts[0];
        let left_inner = &left_pts[0];
        let right_inner_angle = (right_inner[1] - cy).atan2(right_inner[0] - cx);
        let left_inner_angle = (left_inner[1] - cy).atan2(left_inner[0] - cx);

        elements.push(SketchElement::Arc {
            cx,
            cy,
            r: ra,
            start_angle: right_inner_angle.to_degrees(),
            end_angle: left_inner_angle.to_degrees(),
        });

        // Left flank spline (inner to outer)
        elements.push(SketchElement::Spline {
            points: left_pts.clone(),
            degree: 3,
            periodic: false,
            weights: None,
        });

        // Root arc (outer, larger radius) — from left to next right
        let left_outer = left_pts.last().unwrap();
        let left_outer_angle = (left_outer[1] - cy).atan2(left_outer[0] - cx);

        let next_tooth_center = (i + 1) as f64 * tooth_pitch;
        let next_right_angle = next_tooth_center - slot_half_angle;
        let next_start = involute_curve_points(rb, involute_start_r, involute_start_r, 1);
        let (nrx, nry) = rotate_point(next_start[0][0], next_start[0][1], next_right_angle);
        let next_right_inner_angle = nry.atan2(nrx);

        // Right flank of current tooth (outer to inner, reversed)
        let mut right_reversed = right_pts;
        right_reversed.reverse();

        let outer_start = left_outer_angle.to_degrees();
        let mut outer_end = next_right_inner_angle.to_degrees();
        if outer_end < outer_start {
            outer_end += 360.0;
        }
        elements.push(SketchElement::Arc {
            cx,
            cy,
            r: rf,
            start_angle: outer_start,
            end_angle: outer_end,
        });

        // Right flank spline (outer to inner) — only needed for first tooth display
        // Actually for internal gears, we swap: right flank goes outer→inner
        elements.push(SketchElement::Spline {
            points: right_reversed,
            degree: 3,
            periodic: false,
            weights: None,
        });
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
        // 4 elements per tooth: right spline, tip arc, left spline, root arc
        assert_eq!(result.elements.len(), 12 * 4);
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
        // 4 elements: left spline, root arc, right spline, tip arc
        assert_eq!(result.elements.len(), 4);
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
        let p = compute_internal_gear_params(1.0, 30, 20.0, 0.25, 0.0, 0.0);
        let result = internal_gear_profile(&p, [0.0, 0.0], 20);
        // 4 elements per tooth
        assert_eq!(result.elements.len(), 30 * 4);
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
}
