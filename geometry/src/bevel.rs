/// Bevel gear profile generation using Tredgold's approximation.
///
/// Projects teeth onto the back cone and generates involute profiles
/// using a virtual tooth count, producing sketch elements suitable for
/// `cad.sketch` in FreeCAD.

use std::collections::HashMap;
use std::f64::consts::PI;

use crate::involute::{
    half_tooth_angle, involute_curve_points, involute_function,
    mirror_y, rotate_point,
};
use crate::types::{BevelGearParams, SketchElement, SketchResult};

/// Compute bevel gear parameters from module, tooth counts, and shaft angle.
///
/// Uses Tredgold's approximation: the back cone is developed into a plane,
/// and the teeth are designed as if they were spur gear teeth with a virtual
/// tooth count Nv = N / cos(delta).
pub fn compute_bevel_gear_params(
    module: f64,
    teeth: u32,
    mate_teeth: u32,
    pressure_angle_deg: f64,
    shaft_angle_deg: f64,
    face_width: Option<f64>,
) -> BevelGearParams {
    let n = teeth as f64;
    let n_mate = mate_teeth as f64;
    let sigma = shaft_angle_deg.to_radians();

    // Pitch cone angle
    let pitch_cone_angle = if (shaft_angle_deg - 90.0).abs() < 1e-9 {
        (n / n_mate).atan()
    } else {
        (sigma.sin() / (n_mate / n + sigma.cos())).atan()
    };

    let pitch_d = module * n;
    let outer_cone_distance = pitch_d / (2.0 * pitch_cone_angle.sin());

    // Face width: min(R/3, 10*module) if not specified
    let fw = face_width.unwrap_or_else(|| {
        (outer_cone_distance / 3.0).min(10.0 * module)
    });

    let mean_cone_distance = outer_cone_distance - fw / 2.0;
    let virtual_teeth = n / pitch_cone_angle.cos();

    // Diameters at the outer (large) end
    let tip_d = pitch_d + 2.0 * module * pitch_cone_angle.cos();
    let root_d = pitch_d - 2.5 * module * pitch_cone_angle.cos();

    BevelGearParams {
        module,
        teeth,
        mate_teeth,
        pressure_angle_deg,
        shaft_angle_deg,
        pitch_cone_angle_deg: pitch_cone_angle.to_degrees(),
        face_width: fw,
        outer_cone_distance,
        mean_cone_distance,
        virtual_teeth,
        pitch_diameter: pitch_d,
        tip_diameter: tip_d,
        root_diameter: root_d,
    }
}

/// Generate a single bevel gear tooth slot profile at the mean cone.
///
/// Uses Tredgold's approximation: the back cone is unrolled into a plane
/// and an involute tooth is generated using the virtual tooth count.
/// The output is a closed wire (one tooth slot) suitable for pocket +
/// polar_pattern, analogous to `single_tooth_slot` for spur gears.
pub fn bevel_gear_profile(
    params: &BevelGearParams,
    center: [f64; 2],
    num_pts: usize,
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];
    let pa = params.pressure_angle_deg.to_radians();

    // Virtual (Tredgold) spur gear parameters on the back cone
    let nv = params.virtual_teeth;

    // Mean module (scaled by mean/outer cone distance ratio)
    let mean_factor = params.mean_cone_distance / params.outer_cone_distance;
    let mean_module = params.module * mean_factor;

    // Virtual pitch radius on the developed back cone
    let rp = mean_module * nv / 2.0;
    let rb = rp * pa.cos();
    let ra = rp + mean_module; // addendum
    let rf = rp - 1.25 * mean_module; // dedendum

    let involute_start_r = rf.max(rb);
    let needs_radial_lines = rf < rb;

    let inv_pa = involute_function(pa);
    let tooth_half_angle = PI / (2.0 * nv) + inv_pa;

    let tooth_pitch = 2.0 * PI / nv;

    // Generate one tooth slot (gap between two teeth)
    let inv_pts = involute_curve_points(rb, involute_start_r, ra, num_pts);

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

    // Right flank of tooth 1 (from root to tip)
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

    let mut metadata = HashMap::new();
    metadata.insert("module".into(), params.module);
    metadata.insert("teeth".into(), params.teeth as f64);
    metadata.insert("mate_teeth".into(), params.mate_teeth as f64);
    metadata.insert("pressure_angle_deg".into(), params.pressure_angle_deg);
    metadata.insert("shaft_angle_deg".into(), params.shaft_angle_deg);
    metadata.insert("pitch_cone_angle_deg".into(), params.pitch_cone_angle_deg);
    metadata.insert("face_width".into(), params.face_width);
    metadata.insert("outer_cone_distance".into(), params.outer_cone_distance);
    metadata.insert("mean_cone_distance".into(), params.mean_cone_distance);
    metadata.insert("virtual_teeth".into(), params.virtual_teeth);
    metadata.insert("pitch_diameter".into(), params.pitch_diameter);
    metadata.insert("tip_diameter".into(), params.tip_diameter);
    metadata.insert("root_diameter".into(), params.root_diameter);
    metadata.insert("back_cone_pitch_radius".into(), rp);
    metadata.insert("back_cone_tip_radius".into(), ra);
    metadata.insert("back_cone_root_radius".into(), rf);

    SketchResult { elements, metadata }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pitch_cone_angle_90_deg_shaft() {
        let params = compute_bevel_gear_params(2.0, 20, 40, 20.0, 90.0, None);
        // delta = atan(20/40) = atan(0.5) ≈ 26.565°
        let expected = (0.5_f64).atan().to_degrees();
        assert!(
            (params.pitch_cone_angle_deg - expected).abs() < 0.01,
            "pitch_cone_angle: got {}, expected {}",
            params.pitch_cone_angle_deg, expected
        );
    }

    #[test]
    fn test_pitch_cone_angle_equal_teeth() {
        // For equal gears at 90°: delta = atan(1) = 45°
        let params = compute_bevel_gear_params(2.0, 30, 30, 20.0, 90.0, None);
        assert!(
            (params.pitch_cone_angle_deg - 45.0).abs() < 0.01,
            "pitch_cone_angle: got {}, expected 45.0",
            params.pitch_cone_angle_deg
        );
    }

    #[test]
    fn test_virtual_teeth() {
        let params = compute_bevel_gear_params(2.0, 20, 40, 20.0, 90.0, None);
        // Nv = 20 / cos(atan(0.5))
        let delta = (0.5_f64).atan();
        let expected_nv = 20.0 / delta.cos();
        assert!(
            (params.virtual_teeth - expected_nv).abs() < 0.01,
            "virtual_teeth: got {}, expected {}",
            params.virtual_teeth, expected_nv
        );
        // Should be approximately 22.36
        assert!(
            (params.virtual_teeth - 22.36).abs() < 0.1,
            "virtual_teeth ≈ 22.36, got {}",
            params.virtual_teeth
        );
    }

    #[test]
    fn test_pitch_diameter() {
        let params = compute_bevel_gear_params(2.0, 20, 40, 20.0, 90.0, None);
        assert!(
            (params.pitch_diameter - 40.0).abs() < 1e-10,
            "pitch_d: got {}, expected 40.0",
            params.pitch_diameter
        );
    }

    #[test]
    fn test_outer_cone_distance() {
        let params = compute_bevel_gear_params(2.0, 20, 40, 20.0, 90.0, None);
        let delta = (0.5_f64).atan();
        let expected_r = 40.0 / (2.0 * delta.sin());
        assert!(
            (params.outer_cone_distance - expected_r).abs() < 0.01,
            "outer_cone_distance: got {}, expected {}",
            params.outer_cone_distance, expected_r
        );
    }

    #[test]
    fn test_face_width_default() {
        let params = compute_bevel_gear_params(2.0, 20, 40, 20.0, 90.0, None);
        let max_fw = (params.outer_cone_distance / 3.0).min(10.0 * 2.0);
        assert!(
            (params.face_width - max_fw).abs() < 1e-10,
            "face_width: got {}, expected {}",
            params.face_width, max_fw
        );
    }

    #[test]
    fn test_face_width_specified() {
        let params = compute_bevel_gear_params(2.0, 20, 40, 20.0, 90.0, Some(8.0));
        assert!(
            (params.face_width - 8.0).abs() < 1e-10,
            "face_width: got {}, expected 8.0",
            params.face_width
        );
    }

    #[test]
    fn test_non_90_shaft_angle() {
        // For 60° shaft angle with 20T/30T
        let params = compute_bevel_gear_params(2.0, 20, 30, 20.0, 60.0, None);
        let sigma = 60.0_f64.to_radians();
        let expected_delta = (sigma.sin() / (30.0 / 20.0 + sigma.cos())).atan();
        assert!(
            (params.pitch_cone_angle_deg - expected_delta.to_degrees()).abs() < 0.01,
            "non-90 pitch_cone_angle: got {}, expected {}",
            params.pitch_cone_angle_deg, expected_delta.to_degrees()
        );
    }

    #[test]
    fn test_profile_generates_elements() {
        let params = compute_bevel_gear_params(2.0, 20, 40, 20.0, 90.0, None);
        let result = bevel_gear_profile(&params, [0.0, 0.0], 20);
        // Should have at least 4 elements (2 splines + root arc + tip arc)
        assert!(
            result.elements.len() >= 4,
            "expected >= 4 elements, got {}",
            result.elements.len()
        );
    }

    #[test]
    fn test_profile_metadata() {
        let params = compute_bevel_gear_params(2.0, 20, 40, 20.0, 90.0, None);
        let result = bevel_gear_profile(&params, [0.0, 0.0], 20);
        assert!(result.metadata.contains_key("virtual_teeth"));
        assert!(result.metadata.contains_key("pitch_cone_angle_deg"));
        assert!(result.metadata.contains_key("outer_cone_distance"));
        assert!(result.metadata.contains_key("back_cone_pitch_radius"));
    }

    #[test]
    fn test_profile_center_offset() {
        let params = compute_bevel_gear_params(2.0, 20, 40, 20.0, 90.0, None);
        let result = bevel_gear_profile(&params, [10.0, 20.0], 20);
        for elem in &result.elements {
            if let SketchElement::Arc { cx, cy, .. } = elem {
                assert!((cx - 10.0).abs() < 1e-10);
                assert!((cy - 20.0).abs() < 1e-10);
            }
        }
    }

    /// Helper: get the endpoint of a sketch element.
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
    fn test_profile_closed_wire() {
        for (teeth, mate) in [(20, 40), (15, 30), (25, 25), (12, 36)] {
            let params = compute_bevel_gear_params(2.0, teeth, mate, 20.0, 90.0, None);
            let result = bevel_gear_profile(&params, [0.0, 0.0], 20);
            let n = result.elements.len();
            for i in 0..n {
                let end_of_i = element_endpoint(&result.elements[i], 0.0, 0.0, false);
                let start_of_next = element_endpoint(&result.elements[(i + 1) % n], 0.0, 0.0, true);
                let gap = ((end_of_i[0] - start_of_next[0]).powi(2)
                    + (end_of_i[1] - start_of_next[1]).powi(2))
                .sqrt();
                assert!(
                    gap < 1e-4,
                    "Gap of {:.6}mm between elements {} and {} for {}T/{}T bevel gear",
                    gap, i, (i + 1) % n, teeth, mate,
                );
            }
        }
    }
}
