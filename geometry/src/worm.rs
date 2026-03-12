/// Worm gear geometry generation.
///
/// Generates:
/// - Worm thread cross-section (trapezoidal profile for helix sweep)
/// - Wheel tooth profile (involute with normal module correction)
///
/// Key relationships:
/// - lead = worm_starts * axial_module * pi
/// - lead_angle = atan(lead / (pi * worm_pd))
/// - normal_module = axial_module * cos(lead_angle)
/// - efficiency = tan(lambda) / tan(lambda + phi)

use std::collections::HashMap;
use std::f64::consts::PI;

use crate::involute::{
    involute_curve_points, involute_function,
    mirror_y, rotate_point,
};
use crate::types::{SketchElement, SketchResult, WormGearResult};

/// Generate a worm gear pair: worm thread cross-section + wheel tooth profile.
///
/// # Arguments
/// * `axial_module` - Axial module of the worm (mm)
/// * `worm_starts` - Number of worm starts (threads)
/// * `wheel_teeth` - Number of teeth on the worm wheel
/// * `pressure_angle_deg` - Pressure angle (typically 20°)
/// * `worm_pitch_diameter` - Optional worm pitch diameter; auto-sized if None
/// * `center` - Center point for the wheel profile
/// * `num_pts` - Number of points for involute curves
pub fn worm_gear_pair(
    axial_module: f64,
    worm_starts: u32,
    wheel_teeth: u32,
    pressure_angle_deg: f64,
    worm_pitch_diameter: Option<f64>,
    center: [f64; 2],
    num_pts: usize,
) -> WormGearResult {
    let n_w = wheel_teeth as f64;
    let starts = worm_starts as f64;
    let pa = pressure_angle_deg.to_radians();

    // Lead
    let lead = starts * axial_module * PI;

    // Worm pitch diameter: auto-size if not given
    let worm_pd = worm_pitch_diameter.unwrap_or_else(|| {
        axial_module * n_w.powf(0.4) * 2.0
    });

    // Wheel pitch diameter
    let wheel_pd = axial_module * n_w;

    // Center distance
    let center_distance = (worm_pd + wheel_pd) / 2.0;

    // Lead angle
    let lead_angle = (lead / (PI * worm_pd)).atan();
    let lead_angle_deg = lead_angle.to_degrees();

    // Efficiency (typical friction coefficient 0.05)
    let friction_coeff: f64 = 0.05;
    let friction_angle = friction_coeff.atan();
    let efficiency = lead_angle.tan() / (lead_angle + friction_angle).tan();
    let self_locking = lead_angle < friction_angle;

    // --- Worm thread cross-section (trapezoidal, like ACME) ---
    let worm_thread = generate_worm_thread_profile(axial_module, pressure_angle_deg, worm_pd);

    // --- Wheel tooth profile ---
    // Normal module = axial_module * cos(lead_angle)
    let normal_module = axial_module * lead_angle.cos();
    let wheel_profile = generate_wheel_profile(
        normal_module, wheel_teeth, pressure_angle_deg, center, num_pts,
    );

    WormGearResult {
        worm_thread,
        wheel_profile,
        axial_module,
        worm_starts,
        wheel_teeth,
        center_distance,
        lead_angle_deg,
        efficiency,
        self_locking,
        worm_pitch_diameter: worm_pd,
        wheel_pitch_diameter: wheel_pd,
    }
}

/// Generate the worm thread cross-section as a trapezoidal profile.
///
/// This is a 2D profile (in the axial plane) suitable for sweeping along
/// a helix. The trapezoid represents one thread period centered at the
/// worm's pitch line.
///
/// The profile is oriented so X = axial direction, Y = radial direction.
/// Y=0 is at the worm pitch radius.
fn generate_worm_thread_profile(
    axial_module: f64,
    pressure_angle_deg: f64,
    worm_pd: f64,
) -> SketchResult {
    let pa = pressure_angle_deg.to_radians();
    let pitch = axial_module * PI; // axial pitch
    let addendum = axial_module;
    let dedendum = 1.25 * axial_module;
    let worm_r = worm_pd / 2.0;

    // Thread half-thickness at pitch line
    let half_thick_pitch = pitch / 4.0;

    // Flank slope: tan(pa) gives the horizontal run per unit height
    let tan_pa = pa.tan();

    // Crest half-width
    let crest_half = half_thick_pitch - addendum * tan_pa;
    let crest_half = crest_half.max(pitch / 16.0); // minimum crest width

    // Root half-width
    let root_half = half_thick_pitch + dedendum * tan_pa;

    // Profile points (X = axial, Y = radial from center)
    // Start at root-left, go CCW: root-left → crest-left → crest-right → root-right
    let y_root = worm_r - dedendum;
    let y_crest = worm_r + addendum;

    let mut elements = Vec::new();

    // Bottom (root) line: from left root to left crest base
    elements.push(SketchElement::Line {
        x1: -root_half,
        y1: y_root,
        x2: -crest_half,
        y2: y_crest,
    });

    // Top (crest) line
    elements.push(SketchElement::Line {
        x1: -crest_half,
        y1: y_crest,
        x2: crest_half,
        y2: y_crest,
    });

    // Right flank
    elements.push(SketchElement::Line {
        x1: crest_half,
        y1: y_crest,
        x2: root_half,
        y2: y_root,
    });

    // Root line (closing the trapezoid)
    elements.push(SketchElement::Line {
        x1: root_half,
        y1: y_root,
        x2: -root_half,
        y2: y_root,
    });

    let mut metadata = HashMap::new();
    metadata.insert("axial_pitch".into(), pitch);
    metadata.insert("addendum".into(), addendum);
    metadata.insert("dedendum".into(), dedendum);
    metadata.insert("worm_pitch_radius".into(), worm_r);
    metadata.insert("crest_half_width".into(), crest_half);
    metadata.insert("root_half_width".into(), root_half);
    metadata.insert("thread_height".into(), addendum + dedendum);

    SketchResult { elements, metadata }
}

/// Generate the worm wheel tooth slot profile using involute curves.
///
/// The wheel is treated like a spur gear with the normal module
/// (axial_module * cos(lead_angle)) applied.
fn generate_wheel_profile(
    normal_module: f64,
    wheel_teeth: u32,
    pressure_angle_deg: f64,
    center: [f64; 2],
    num_pts: usize,
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];
    let pa = pressure_angle_deg.to_radians();
    let z = wheel_teeth as f64;

    let pitch_d = normal_module * z;
    let base_d = pitch_d * pa.cos();
    let tip_d = normal_module * (z + 2.0);
    let root_d = normal_module * (z - 2.5);

    let rb = base_d / 2.0;
    let ra = tip_d / 2.0;
    let rf = root_d / 2.0;

    let involute_start_r = rf.max(rb);
    let needs_radial_lines = rf < rb;

    let inv_pa = involute_function(pa);
    let tooth_half_angle = PI / (2.0 * z) + inv_pa;

    let tooth_pitch = 2.0 * PI / z;

    // Generate one tooth slot
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

    let right_angle_next = tooth_pitch - tooth_half_angle;
    let right_pts: Vec<[f64; 2]> = inv_pts
        .iter()
        .map(|p| {
            let (rx, ry) = rotate_point(p[0], p[1], right_angle_next);
            [rx + cx, ry + cy]
        })
        .collect();

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
    metadata.insert("normal_module".into(), normal_module);
    metadata.insert("wheel_teeth".into(), wheel_teeth as f64);
    metadata.insert("pitch_diameter".into(), pitch_d);
    metadata.insert("base_diameter".into(), base_d);
    metadata.insert("tip_diameter".into(), tip_d);
    metadata.insert("root_diameter".into(), root_d);

    SketchResult { elements, metadata }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basic_worm_pair() {
        let result = worm_gear_pair(2.0, 1, 40, 20.0, None, [0.0, 0.0], 20);
        assert_eq!(result.worm_starts, 1);
        assert_eq!(result.wheel_teeth, 40);
        assert!((result.wheel_pitch_diameter - 80.0).abs() < 1e-10);
    }

    #[test]
    fn test_lead_angle() {
        // Single-start, axial_module=2, specified worm_pd=20
        let result = worm_gear_pair(2.0, 1, 40, 20.0, Some(20.0), [0.0, 0.0], 20);
        // lead = 1 * 2 * pi = 6.2832
        // lead_angle = atan(6.2832 / (pi * 20)) = atan(0.1) ≈ 5.71°
        let expected_lead = 1.0 * 2.0 * PI;
        let expected_angle = (expected_lead / (PI * 20.0)).atan().to_degrees();
        assert!(
            (result.lead_angle_deg - expected_angle).abs() < 0.01,
            "lead_angle: got {}, expected {}",
            result.lead_angle_deg, expected_angle
        );
    }

    #[test]
    fn test_center_distance() {
        let result = worm_gear_pair(2.0, 1, 40, 20.0, Some(20.0), [0.0, 0.0], 20);
        let expected = (20.0 + 80.0) / 2.0;
        assert!(
            (result.center_distance - expected).abs() < 1e-10,
            "center_distance: got {}, expected {}",
            result.center_distance, expected
        );
    }

    #[test]
    fn test_multi_start_lead() {
        // 3-start worm
        let result = worm_gear_pair(2.0, 3, 40, 20.0, Some(30.0), [0.0, 0.0], 20);
        // lead = 3 * 2 * pi = 18.85
        // lead_angle = atan(18.85 / (pi * 30)) = atan(0.2) ≈ 11.31°
        let expected_angle = (3.0 * 2.0 * PI / (PI * 30.0)).atan().to_degrees();
        assert!(
            (result.lead_angle_deg - expected_angle).abs() < 0.01,
            "multi-start lead_angle: got {}, expected {}",
            result.lead_angle_deg, expected_angle
        );
    }

    #[test]
    fn test_self_locking() {
        // Single start with small worm pd → small lead angle → self-locking
        let result = worm_gear_pair(1.0, 1, 60, 20.0, Some(40.0), [0.0, 0.0], 20);
        // lead = pi, lead_angle = atan(pi / (pi*40)) = atan(0.025) ≈ 1.43°
        // friction_angle = atan(0.05) ≈ 2.86°
        // 1.43° < 2.86° → self-locking
        assert!(result.self_locking, "single-start worm with large pd should self-lock");
    }

    #[test]
    fn test_efficiency_range() {
        let result = worm_gear_pair(2.0, 3, 40, 20.0, Some(20.0), [0.0, 0.0], 20);
        assert!(
            result.efficiency > 0.0 && result.efficiency < 1.0,
            "efficiency should be 0..1, got {}",
            result.efficiency
        );
    }

    #[test]
    fn test_worm_thread_profile_4_lines() {
        let result = worm_gear_pair(2.0, 1, 40, 20.0, Some(20.0), [0.0, 0.0], 20);
        // Worm thread profile is a trapezoid = 4 lines
        let line_count = result.worm_thread.elements.iter().filter(|e| {
            matches!(e, SketchElement::Line { .. })
        }).count();
        assert_eq!(line_count, 4, "worm thread should have 4 lines (trapezoid)");
    }

    #[test]
    fn test_worm_thread_closed() {
        let result = worm_gear_pair(2.0, 1, 40, 20.0, Some(20.0), [0.0, 0.0], 20);
        let elems = &result.worm_thread.elements;
        let n = elems.len();
        for i in 0..n {
            let end = thread_endpoint(&elems[i], false);
            let start = thread_endpoint(&elems[(i + 1) % n], true);
            let gap = ((end[0] - start[0]).powi(2) + (end[1] - start[1]).powi(2)).sqrt();
            assert!(
                gap < 1e-10,
                "worm thread gap of {} between elements {} and {}",
                gap, i, (i + 1) % n,
            );
        }
    }

    #[test]
    fn test_wheel_profile_has_elements() {
        let result = worm_gear_pair(2.0, 1, 40, 20.0, None, [0.0, 0.0], 20);
        assert!(
            result.wheel_profile.elements.len() >= 4,
            "wheel profile should have >= 4 elements, got {}",
            result.wheel_profile.elements.len()
        );
    }

    #[test]
    fn test_wheel_profile_center_offset() {
        let result = worm_gear_pair(2.0, 1, 40, 20.0, None, [5.0, 10.0], 20);
        for elem in &result.wheel_profile.elements {
            if let SketchElement::Arc { cx, cy, .. } = elem {
                assert!((cx - 5.0).abs() < 1e-10);
                assert!((cy - 10.0).abs() < 1e-10);
            }
        }
    }

    #[test]
    fn test_auto_worm_pd() {
        // When worm_pd is None, it should be auto-sized
        let result = worm_gear_pair(2.0, 1, 40, 20.0, None, [0.0, 0.0], 20);
        assert!(
            result.worm_pitch_diameter > 0.0,
            "auto worm_pd should be positive"
        );
        // Auto formula: axial_module * wheel_teeth^0.4 * 2
        let expected = 2.0 * (40.0_f64).powf(0.4) * 2.0;
        assert!(
            (result.worm_pitch_diameter - expected).abs() < 1e-10,
            "auto worm_pd: got {}, expected {}",
            result.worm_pitch_diameter, expected
        );
    }

    /// Helper for thread profile endpoint extraction.
    fn thread_endpoint(elem: &SketchElement, start: bool) -> [f64; 2] {
        match elem {
            SketchElement::Line { x1, y1, x2, y2 } => {
                if start { [*x1, *y1] } else { [*x2, *y2] }
            }
            _ => panic!("worm thread should only have lines"),
        }
    }
}
