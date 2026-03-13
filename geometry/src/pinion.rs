/// Epicycloidal gear tooth profile generation.
///
/// Generates tooth profiles using epicycloidal, ogival, or modified involute
/// curves. These profiles are preferred over standard involute for low tooth
/// counts (< ~20) where involute profiles undercut badly. Common in clocks,
/// watches, instruments, cycloidal drives, and precision mechanisms.

use std::f64::consts::PI;

use crate::involute::rotate_point;
use crate::types::{EpicycloidalGearParams, SketchElement, SketchResult};

/// Compute epicycloidal gear parameters.
pub fn compute_epicycloidal_gear_params(
    module: f64,
    teeth: u32,
    mating_teeth: u32,
    profile_type: &str,
    pressure_angle_deg: f64,
    addendum_coeff: f64,
    dedendum_coeff: f64,
    _backlash: f64,
) -> EpicycloidalGearParams {
    let z = teeth as f64;
    let pitch_d = module * z;
    let tip_d = pitch_d + 2.0 * module * addendum_coeff;
    let root_d = pitch_d - 2.0 * module * dedendum_coeff;

    // Mating clearance: gap between this gear's tip and mating gear's root
    let mating_root_d = module * mating_teeth as f64 - 2.0 * module * 1.25;
    let center_dist = module * (teeth as f64 + mating_teeth as f64) / 2.0;
    let mating_clearance = center_dist - tip_d / 2.0 - mating_root_d / 2.0;

    EpicycloidalGearParams {
        module,
        teeth,
        profile_type: profile_type.to_string(),
        pressure_angle_deg,
        addendum_coeff,
        dedendum_coeff,
        pitch_diameter: pitch_d,
        tip_diameter: tip_d,
        root_diameter: root_d.max(0.1),
        mating_teeth,
        mating_clearance: mating_clearance.max(0.0),
    }
}

/// Generate an epicycloidal point on a circle of radius `gen_r` rolling
/// on the outside of a base circle of radius `base_r`.
fn epicycloid_point(base_r: f64, gen_r: f64, phi: f64) -> (f64, f64) {
    let ratio = base_r / gen_r + 1.0;
    let x = (base_r + gen_r) * phi.cos() - gen_r * (ratio * phi).cos();
    let y = (base_r + gen_r) * phi.sin() - gen_r * (ratio * phi).sin();
    (x, y)
}

/// Generate an ogival (two-arc) tooth flank profile.
fn ogival_flank(
    _rp: f64,
    ra: f64,
    rf: f64,
    half_tooth_angle: f64,
    num_points: usize,
) -> Vec<[f64; 2]> {
    let mut points = Vec::with_capacity(num_points);
    for i in 0..num_points {
        let frac = i as f64 / (num_points - 1) as f64;
        let r = rf + (ra - rf) * frac;
        let max_half_angle = half_tooth_angle * (1.0 - 0.3 * frac);
        let angle = max_half_angle * (1.0 - frac * frac);
        points.push([r * angle.cos(), r * angle.sin()]);
    }
    points
}

/// Generate a single tooth slot profile for pocket + polar_pattern workflow.
///
/// Returns a closed wire describing the gap between two adjacent teeth,
/// suitable for FreeCAD pocket operations.
pub fn epicycloidal_tooth_slot(
    params: &EpicycloidalGearParams,
    center: [f64; 2],
    tip_rounding_r: f64,
    root_fillet_r: f64,
    num_points: usize,
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];
    let rp = params.pitch_diameter / 2.0;
    let ra = params.tip_diameter / 2.0;
    let rf = params.root_diameter / 2.0;
    let z = params.teeth;
    let tooth_pitch = 2.0 * PI / z as f64;
    let half_tooth = tooth_pitch / 2.0;

    // Generate the right flank profile (addendum part)
    let right_flank_pts = match params.profile_type.as_str() {
        "epicycloidal" => {
            let mating_rp = params.module * params.mating_teeth as f64 / 2.0;
            let gen_r = mating_rp / 2.0;

            let mut pts = Vec::with_capacity(num_points);
            let max_phi = ((ra - rp) / gen_r).min(PI / z as f64);

            for i in 0..num_points {
                let frac = i as f64 / (num_points - 1) as f64;
                let phi = frac * max_phi;
                let (ex, ey) = epicycloid_point(rp, gen_r, phi);
                let r = (ex * ex + ey * ey).sqrt();
                let a = ey.atan2(ex);
                pts.push([r * a.cos(), r * a.sin()]);
            }
            pts
        }
        "ogival" => ogival_flank(rp, ra, rf, half_tooth * 0.4, num_points),
        _ => {
            // "modified_involute": standard involute with reduced addendum
            let pa = params.pressure_angle_deg.to_radians();
            let rb = rp * pa.cos();
            let inv_start_r = rf.max(rb);

            let mut pts = Vec::with_capacity(num_points);
            let t_start = if inv_start_r <= rb {
                0.0
            } else {
                ((inv_start_r / rb).powi(2) - 1.0).sqrt()
            };
            let t_end = if ra <= rb {
                0.0
            } else {
                ((ra / rb).powi(2) - 1.0).sqrt()
            };

            for i in 0..num_points {
                let frac = i as f64 / (num_points - 1) as f64;
                let t = t_start + frac * (t_end - t_start);
                let x = rb * (t.cos() + t * t.sin());
                let y = rb * (t.sin() - t * t.cos());
                pts.push([x, y]);
            }
            pts
        }
    };

    // Left flank (mirror of right)
    let left_flank_pts: Vec<[f64; 2]> = right_flank_pts
        .iter()
        .map(|p| [p[0], -p[1]])
        .collect();

    // Build closed slot: left flank of tooth 0 → root arc → right flank of tooth 1 → tip arc
    let left_angle = half_tooth;
    let right_angle_next = tooth_pitch - half_tooth;

    let left_pts: Vec<[f64; 2]> = left_flank_pts
        .iter()
        .rev()
        .map(|p| {
            let (rx, ry) = rotate_point(p[0], p[1], left_angle);
            [rx + cx, ry + cy]
        })
        .collect();

    let right_pts: Vec<[f64; 2]> = right_flank_pts
        .iter()
        .map(|p| {
            let (rx, ry) = rotate_point(p[0], p[1], right_angle_next);
            [rx + cx, ry + cy]
        })
        .collect();

    let mut elements = Vec::new();

    // Left flank spline (tip to root)
    elements.push(SketchElement::Spline {
        points: left_pts.clone(),
        degree: 3,
        periodic: false,
        weights: None,
    });

    // Root arc
    let left_root = left_pts.last().unwrap();
    let right_root = &right_pts[0];
    let left_root_angle = (left_root[1] - cy).atan2(left_root[0] - cx);
    let right_root_angle = (right_root[1] - cy).atan2(right_root[0] - cx);

    let root_r = if root_fillet_r > 0.0 { rf + root_fillet_r } else { rf };
    let mut root_end_deg = right_root_angle.to_degrees();
    let root_start_deg = left_root_angle.to_degrees();
    if root_end_deg < root_start_deg {
        root_end_deg += 360.0;
    }
    elements.push(SketchElement::Arc {
        cx, cy, r: root_r,
        start_angle: root_start_deg,
        end_angle: root_end_deg,
    });

    // Right flank spline (root to tip)
    elements.push(SketchElement::Spline {
        points: right_pts.clone(),
        degree: 3,
        periodic: false,
        weights: None,
    });

    // Tip arc closing the slot
    let right_tip = right_pts.last().unwrap();
    let left_tip = &left_pts[0];
    let right_tip_angle = (right_tip[1] - cy).atan2(right_tip[0] - cx);
    let left_tip_angle = (left_tip[1] - cy).atan2(left_tip[0] - cx);

    let tip_r = if tip_rounding_r > 0.0 { ra - tip_rounding_r } else { ra };
    let tip_start_deg = right_tip_angle.to_degrees();
    let mut tip_end_deg = left_tip_angle.to_degrees();
    if tip_end_deg < tip_start_deg {
        tip_end_deg += 360.0;
    }
    elements.push(SketchElement::Arc {
        cx, cy, r: tip_r.max(ra * 0.95),
        start_angle: tip_start_deg,
        end_angle: tip_end_deg,
    });

    SketchResult {
        elements,
        metadata: params.to_metadata(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_epicycloidal_gear_params() {
        let p = compute_epicycloidal_gear_params(0.1, 8, 80, "epicycloidal", 15.0, 0.75, 0.85, 0.0);
        assert_eq!(p.teeth, 8);
        assert!((p.pitch_diameter - 0.8).abs() < 1e-10);
        assert!(p.tip_diameter > p.pitch_diameter);
        assert!(p.root_diameter < p.pitch_diameter);
    }

    #[test]
    fn test_epicycloidal_tooth_slot() {
        let p = compute_epicycloidal_gear_params(0.1, 8, 80, "epicycloidal", 15.0, 0.75, 0.85, 0.0);
        let result = epicycloidal_tooth_slot(&p, [0.0, 0.0], 0.0, 0.0, 20);
        assert_eq!(result.elements.len(), 4);
    }

    #[test]
    fn test_ogival_tooth_slot() {
        let p = compute_epicycloidal_gear_params(0.1, 10, 80, "ogival", 15.0, 0.75, 0.85, 0.0);
        let result = epicycloidal_tooth_slot(&p, [0.0, 0.0], 0.0, 0.0, 20);
        assert_eq!(result.elements.len(), 4);
    }

    #[test]
    fn test_modified_involute_tooth_slot() {
        let p = compute_epicycloidal_gear_params(0.1, 12, 80, "modified_involute", 15.0, 0.75, 0.85, 0.0);
        let result = epicycloidal_tooth_slot(&p, [0.0, 0.0], 0.0, 0.0, 20);
        assert_eq!(result.elements.len(), 4);
    }

    #[test]
    fn test_center_offset() {
        let p = compute_epicycloidal_gear_params(0.1, 8, 80, "epicycloidal", 15.0, 0.75, 0.85, 0.0);
        let result = epicycloidal_tooth_slot(&p, [5.0, 10.0], 0.0, 0.0, 20);
        for elem in &result.elements {
            if let SketchElement::Arc { cx, cy, .. } = elem {
                assert!((cx - 5.0).abs() < 1e-10);
                assert!((cy - 10.0).abs() < 1e-10);
            }
        }
    }

    #[test]
    fn test_various_tooth_counts() {
        for teeth in [4, 6, 8, 10, 12, 15, 20, 30] {
            let p = compute_epicycloidal_gear_params(0.1, teeth, 80, "epicycloidal", 15.0, 0.75, 0.85, 0.0);
            let result = epicycloidal_tooth_slot(&p, [0.0, 0.0], 0.0, 0.0, 20);
            assert_eq!(result.elements.len(), 4, "Expected 4 elements for {} teeth", teeth);
        }
    }

    #[test]
    fn test_epicycloid_point_at_zero() {
        let (x, y) = epicycloid_point(10.0, 5.0, 0.0);
        assert!((x - 10.0).abs() < 1e-10);
        assert!(y.abs() < 1e-10);
    }
}
