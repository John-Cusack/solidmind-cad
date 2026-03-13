/// Archimedean spiral geometry generation with optional spring analysis.
///
/// Generates flat spirals useful for springs (clock, return, constant-force),
/// scroll compressor profiles, spiral cams, decorative patterns, etc.
/// Optional spring analysis computes stiffness and bending stress when
/// strip cross-section and material properties are provided.

use std::collections::HashMap;
use std::f64::consts::PI;

use crate::types::{SketchElement, SketchResult, SpiralResult};

// ---------------------------------------------------------------------------
// Archimedean spiral generation
// ---------------------------------------------------------------------------

/// Generate points along an Archimedean spiral: r(θ) = a + b·θ
/// where a = inner_radius and b = (outer_radius - inner_radius) / (2π·num_turns).
fn archimedean_spiral_points(
    inner_radius: f64,
    outer_radius: f64,
    num_turns: f64,
    num_points_per_turn: usize,
    center_x: f64,
    center_y: f64,
) -> Vec<[f64; 2]> {
    let total_angle = 2.0 * PI * num_turns;
    let a = inner_radius;
    let b = (outer_radius - inner_radius) / total_angle;
    let total_points = (num_turns * num_points_per_turn as f64).ceil() as usize;
    let total_points = total_points.max(2);

    let mut points = Vec::with_capacity(total_points);
    for i in 0..total_points {
        let frac = i as f64 / (total_points - 1) as f64;
        let theta = frac * total_angle;
        let r = a + b * theta;
        points.push([center_x + r * theta.cos(), center_y + r * theta.sin()]);
    }
    points
}

/// Compute developed length of an Archimedean spiral via numerical integration.
fn archimedean_developed_length(inner_radius: f64, outer_radius: f64, num_turns: f64) -> f64 {
    let total_angle = 2.0 * PI * num_turns;
    let a = inner_radius;
    let b = (outer_radius - inner_radius) / total_angle;

    // Simpson's rule
    let n = 1000;
    let h = total_angle / n as f64;
    let mut sum = 0.0;
    for i in 0..=n {
        let theta = i as f64 * h;
        let r = a + b * theta;
        let ds = (r * r + b * b).sqrt();
        let w = if i == 0 || i == n {
            1.0
        } else if i % 2 == 1 {
            4.0
        } else {
            2.0
        };
        sum += w * ds;
    }
    sum * h / 3.0
}

// ---------------------------------------------------------------------------
// Terminal curve (overcoil)
// ---------------------------------------------------------------------------

/// Generate a terminal curve at the outer end of the spiral.
///
/// Styles:
/// - `"simple"`: circular arc curving inward then back out
/// - `"phillips"`: smooth cubic curve (Phillips terminal curve) that keeps
///    the center of gravity approximately stationary during oscillation
fn terminal_curve_points(
    outer_radius: f64,
    overcoil_angle_deg: f64,
    style: &str,
    num_points: usize,
    center_x: f64,
    center_y: f64,
) -> Vec<[f64; 2]> {
    let overcoil_angle = overcoil_angle_deg.to_radians();
    let num_points = num_points.max(2);
    let mut points = Vec::with_capacity(num_points);

    let rise_radius = outer_radius * 0.65;
    let stud_radius = outer_radius * 0.85;

    for i in 0..num_points {
        let frac = i as f64 / (num_points - 1) as f64;
        let angle = frac * overcoil_angle;

        let r = if style == "phillips" {
            // Smooth cubic interpolation (Phillips terminal curve)
            let t = frac;
            let t1 = 1.0 - t;
            outer_radius * t1 * t1 * t1
                + rise_radius * 3.0 * t * t1 * t1
                + rise_radius * 3.0 * t * t * t1
                + stud_radius * t * t * t
        } else {
            // Simple circular overcoil
            let mid = (outer_radius + rise_radius) / 2.0;
            let amp = (outer_radius - rise_radius) / 2.0;
            mid + amp * (PI * frac).cos()
        };

        points.push([center_x + r * angle.cos(), center_y + r * angle.sin()]);
    }
    points
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Generate an Archimedean spiral with optional spring analysis and terminal curve.
///
/// **Base mode** (geometry only): provide `inner_radius`, `outer_radius`, `num_turns`.
///
/// **Spring analysis**: additionally provide `strip_thickness_mm` and `strip_height_mm`
/// to compute stiffness. Add `material_e_gpa` and `material_yield_mpa` to compute
/// bending stress and get a pass/fail check.
///
/// **Terminal curve**: provide `overcoil_angle_deg` > 0 with `overcoil_style`
/// ("simple" or "phillips") to generate a terminal curve at the outer end.
pub fn spiral(
    inner_radius: f64,
    outer_radius: f64,
    num_turns: f64,
    num_points_per_turn: usize,
    center_x: f64,
    center_y: f64,
    // Optional spring analysis
    strip_thickness_mm: Option<f64>,
    strip_height_mm: Option<f64>,
    material_e_gpa: Option<f64>,
    material_yield_mpa: Option<f64>,
    // Optional terminal curve
    overcoil_angle_deg: Option<f64>,
    overcoil_style: Option<&str>,
) -> SpiralResult {
    // Generate spiral points
    let spiral_pts = archimedean_spiral_points(
        inner_radius, outer_radius, num_turns,
        num_points_per_turn, center_x, center_y,
    );

    let spiral = SketchResult {
        elements: vec![SketchElement::Spline {
            points: spiral_pts,
            degree: 3,
            periodic: false,
            weights: None,
        }],
        metadata: {
            let mut m = HashMap::new();
            m.insert("inner_radius".into(), inner_radius);
            m.insert("outer_radius".into(), outer_radius);
            m.insert("num_turns".into(), num_turns);
            m
        },
    };

    // Terminal curve
    let overcoil = match overcoil_angle_deg {
        Some(angle) if angle > 0.0 => {
            let style = overcoil_style.unwrap_or("simple");
            let oc_pts = terminal_curve_points(
                outer_radius, angle, style,
                (num_points_per_turn as f64 * angle / 360.0).ceil() as usize + 2,
                center_x, center_y,
            );
            Some(SketchResult {
                elements: vec![SketchElement::Spline {
                    points: oc_pts,
                    degree: 3,
                    periodic: false,
                    weights: None,
                }],
                metadata: {
                    let mut m = HashMap::new();
                    m.insert("overcoil_angle_deg".into(), angle);
                    m
                },
            })
        }
        _ => None,
    };

    let developed_length = archimedean_developed_length(inner_radius, outer_radius, num_turns);

    // Spring analysis (optional)
    let stiffness = match (strip_thickness_mm, strip_height_mm, material_e_gpa) {
        (Some(h), Some(b), Some(e_gpa)) => {
            let e_pa = e_gpa * 1e9;
            let dev_m = developed_length * 1e-3;
            let h_m = h * 1e-3;
            let b_m = b * 1e-3;
            if dev_m > 0.0 {
                Some(e_pa * b_m * h_m.powi(3) / (12.0 * dev_m))
            } else {
                Some(0.0)
            }
        }
        _ => None,
    };

    let (wall_stress, stress_ok) = match (strip_thickness_mm, material_e_gpa, material_yield_mpa) {
        (Some(h), Some(e_gpa), Some(yield_mpa)) => {
            let e_pa = e_gpa * 1e9;
            let h_m = h * 1e-3;
            let r_avg_m = (inner_radius + outer_radius) / 2.0 * 1e-3;
            if r_avg_m > 0.0 {
                let stress_mpa = (e_pa * h_m / (2.0 * r_avg_m)) * 1e-6;
                (Some(stress_mpa), Some(stress_mpa < yield_mpa))
            } else {
                (Some(0.0), Some(true))
            }
        }
        _ => (None, None),
    };

    SpiralResult {
        spiral,
        overcoil,
        developed_length_mm: developed_length,
        num_turns,
        inner_radius,
        outer_radius,
        stiffness_n_m_per_rad: stiffness,
        wall_stress_mpa: wall_stress,
        stress_ok,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_spiral_basic_geometry() {
        let result = spiral(2.0, 8.0, 5.0, 30, 0.0, 0.0, None, None, None, None, None, None);
        assert!(!result.spiral.elements.is_empty());
        assert!(result.overcoil.is_none());
        assert!(result.developed_length_mm > 0.0);
        assert!(result.stiffness_n_m_per_rad.is_none());
        assert!(result.wall_stress_mpa.is_none());
    }

    #[test]
    fn test_spiral_start_end_radius() {
        let result = spiral(2.0, 8.0, 5.0, 30, 0.0, 0.0, None, None, None, None, None, None);
        if let crate::types::SketchElement::Spline { points, .. } = &result.spiral.elements[0] {
            let r_start = (points[0][0].powi(2) + points[0][1].powi(2)).sqrt();
            let r_end = (points.last().unwrap()[0].powi(2) + points.last().unwrap()[1].powi(2)).sqrt();
            assert!((r_start - 2.0).abs() < 0.01);
            assert!((r_end - 8.0).abs() < 0.01);
        }
    }

    #[test]
    fn test_developed_length_positive() {
        let len = archimedean_developed_length(2.0, 8.0, 10.0);
        assert!(len > 0.0);
        let min_len = 2.0 * PI * 2.0 * 10.0;
        let max_len = 2.0 * PI * 8.0 * 10.0;
        assert!(len > min_len);
        assert!(len < max_len);
    }

    #[test]
    fn test_spiral_with_spring_analysis() {
        let result = spiral(
            0.5, 5.0, 12.0, 20, 0.0, 0.0,
            Some(0.05), Some(0.15), Some(210.0), Some(900.0),
            None, None,
        );
        assert!(result.stiffness_n_m_per_rad.unwrap() > 0.0);
        assert!(result.wall_stress_mpa.is_some());
        assert!(result.stress_ok.is_some());
    }

    #[test]
    fn test_spiral_with_terminal_curve() {
        let result = spiral(
            0.5, 5.0, 12.0, 20, 0.0, 0.0,
            None, None, None, None,
            Some(270.0), Some("phillips"),
        );
        assert!(result.overcoil.is_some());
    }

    #[test]
    fn test_spiral_simple_terminal_curve() {
        let result = spiral(
            0.5, 5.0, 12.0, 20, 0.0, 0.0,
            None, None, None, None,
            Some(180.0), Some("simple"),
        );
        assert!(result.overcoil.is_some());
    }

    #[test]
    fn test_spiral_stress_thin_spring_ok() {
        let result = spiral(
            1.0, 10.0, 8.0, 20, 0.0, 0.0,
            Some(0.05), Some(0.15), Some(210.0), Some(2000.0),
            None, None,
        );
        assert!(result.stress_ok.unwrap(), "thin spring in large radius should pass");
    }

    #[test]
    fn test_spiral_stress_thick_spring_fails() {
        let result = spiral(
            0.5, 1.5, 3.0, 20, 0.0, 0.0,
            Some(0.5), Some(1.0), Some(210.0), Some(500.0),
            None, None,
        );
        // Thick strip in tight radius should produce high stress
        assert!(result.wall_stress_mpa.unwrap() > 500.0);
    }
}
