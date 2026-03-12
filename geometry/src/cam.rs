/// Cam profile generation — produces a closed spline outline for disc cams
/// with configurable motion laws and follower types.

use std::collections::HashMap;
use std::f64::consts::PI;

use crate::types::{CamResult, CamSegment, SketchElement, SketchResult};

// ---------------------------------------------------------------------------
// Motion law functions — displacement s(β) for β ∈ [0, 1], rise h
// ---------------------------------------------------------------------------

fn motion_dwell(_beta: f64, _h: f64) -> f64 {
    0.0
}

fn motion_simple_harmonic(beta: f64, h: f64) -> f64 {
    h / 2.0 * (1.0 - (PI * beta).cos())
}

fn motion_cycloidal(beta: f64, h: f64) -> f64 {
    h * (beta - (2.0 * PI * beta).sin() / (2.0 * PI))
}

fn motion_polynomial345(beta: f64, h: f64) -> f64 {
    h * (10.0 * beta.powi(3) - 15.0 * beta.powi(4) + 6.0 * beta.powi(5))
}

fn motion_polynomial4567(beta: f64, h: f64) -> f64 {
    h * (35.0 * beta.powi(4) - 84.0 * beta.powi(5) + 70.0 * beta.powi(6)
        - 20.0 * beta.powi(7))
}

fn motion_constant_velocity(beta: f64, h: f64) -> f64 {
    h * beta
}

/// Velocity (ds/dβ) for each motion law — needed for pressure angle.
fn velocity_simple_harmonic(beta: f64, h: f64) -> f64 {
    h * PI / 2.0 * (PI * beta).sin()
}

fn velocity_cycloidal(beta: f64, h: f64) -> f64 {
    h * (1.0 - (2.0 * PI * beta).cos())
}

fn velocity_polynomial345(beta: f64, h: f64) -> f64 {
    h * (30.0 * beta.powi(2) - 60.0 * beta.powi(3) + 30.0 * beta.powi(4))
}

fn velocity_polynomial4567(beta: f64, h: f64) -> f64 {
    h * (140.0 * beta.powi(3) - 420.0 * beta.powi(4) + 420.0 * beta.powi(5)
        - 140.0 * beta.powi(6))
}

fn velocity_constant_velocity(_beta: f64, h: f64) -> f64 {
    h
}

/// Acceleration (d²s/dβ²) for each motion law.
fn accel_dwell(_beta: f64, _h: f64) -> f64 {
    0.0
}

fn accel_simple_harmonic(beta: f64, h: f64) -> f64 {
    // s  = h/2 * (1 - cos(πβ))
    // ds/dβ = h*π/2 * sin(πβ)
    // d²s/dβ² = h*π²/2 * cos(πβ)
    h * PI * PI / 2.0 * (PI * beta).cos()
}

fn accel_cycloidal(beta: f64, h: f64) -> f64 {
    h * 2.0 * PI * (2.0 * PI * beta).sin()
}

fn accel_polynomial345(beta: f64, h: f64) -> f64 {
    h * (60.0 * beta - 180.0 * beta.powi(2) + 120.0 * beta.powi(3))
}

fn accel_polynomial4567(beta: f64, h: f64) -> f64 {
    h * (420.0 * beta.powi(2) - 1680.0 * beta.powi(3) + 2100.0 * beta.powi(4)
        - 840.0 * beta.powi(5))
}

fn accel_constant_velocity(_beta: f64, _h: f64) -> f64 {
    0.0
}

/// Select the displacement function for a given motion law string.
fn select_displacement(law: &str) -> Result<fn(f64, f64) -> f64, String> {
    match law {
        "dwell" => Ok(motion_dwell),
        "simple_harmonic" => Ok(motion_simple_harmonic),
        "cycloidal" => Ok(motion_cycloidal),
        "polynomial345" => Ok(motion_polynomial345),
        "polynomial4567" => Ok(motion_polynomial4567),
        "constant_velocity" => Ok(motion_constant_velocity),
        _ => Err(format!(
            "Unknown motion_law '{}'. Valid: dwell, simple_harmonic, cycloidal, \
             polynomial345, polynomial4567, constant_velocity",
            law
        )),
    }
}

fn select_velocity(law: &str) -> fn(f64, f64) -> f64 {
    match law {
        "simple_harmonic" => velocity_simple_harmonic,
        "cycloidal" => velocity_cycloidal,
        "polynomial345" => velocity_polynomial345,
        "polynomial4567" => velocity_polynomial4567,
        "constant_velocity" => velocity_constant_velocity,
        _ => |_b, _h| 0.0, // dwell
    }
}

fn select_acceleration(law: &str) -> fn(f64, f64) -> f64 {
    match law {
        "dwell" => accel_dwell,
        "simple_harmonic" => accel_simple_harmonic,
        "cycloidal" => accel_cycloidal,
        "polynomial345" => accel_polynomial345,
        "polynomial4567" => accel_polynomial4567,
        "constant_velocity" => accel_constant_velocity,
        _ => |_b, _h| 0.0,
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Generate a cam profile as a closed periodic spline.
///
/// # Arguments
/// * `base_radius` — base circle radius (mm)
/// * `segments` — cam segments covering 360 degrees
/// * `follower_type` — "knife_edge", "roller", or "flat_face"
/// * `follower_radius` — roller radius (mm), 0 for knife_edge / flat_face
/// * `center_x`, `center_y` — cam center position
/// * `num_points_per_segment` — angular resolution per segment
pub fn cam_profile(
    base_radius: f64,
    segments: &[CamSegment],
    follower_type: &str,
    follower_radius: f64,
    center_x: f64,
    center_y: f64,
    num_points_per_segment: usize,
) -> Result<CamResult, String> {
    // --- Validation ---
    if base_radius <= 0.0 {
        return Err("base_radius must be positive".into());
    }
    if segments.is_empty() {
        return Err("segments must not be empty".into());
    }
    let valid_followers = ["knife_edge", "roller", "flat_face"];
    if !valid_followers.contains(&follower_type) {
        return Err(format!(
            "follower_type must be one of {:?}, got '{}'",
            valid_followers, follower_type
        ));
    }
    if follower_type == "roller" && follower_radius <= 0.0 {
        return Err("follower_radius must be positive for roller follower".into());
    }
    if num_points_per_segment < 2 {
        return Err("num_points_per_segment must be >= 2".into());
    }

    // Validate segment coverage
    let mut sorted_segs: Vec<&CamSegment> = segments.iter().collect();
    sorted_segs.sort_by(|a, b| a.start_angle_deg.partial_cmp(&b.start_angle_deg).unwrap());

    let total_span: f64 = sorted_segs
        .iter()
        .map(|s| {
            let mut span = s.end_angle_deg - s.start_angle_deg;
            if span < 0.0 {
                span += 360.0;
            }
            span
        })
        .sum();

    if (total_span - 360.0).abs() > 0.5 {
        return Err(format!(
            "Segments must cover exactly 360 degrees, got {:.1} degrees",
            total_span
        ));
    }

    // Validate all motion laws upfront
    for seg in segments {
        select_displacement(&seg.motion_law)?;
    }

    // --- Build pitch curve (displacement vs angle) ---
    // Walk segments in angular order, accumulate displacement.
    let mut cumulative_displacement = 0.0;
    let mut pitch_points: Vec<(f64, f64, f64)> = Vec::new(); // (theta_rad, r, ds_dtheta)
    let mut displacement_curve: Vec<[f64; 2]> = Vec::new();
    let mut max_accel: f64 = 0.0;

    for seg in &sorted_segs {
        let disp_fn = select_displacement(&seg.motion_law).unwrap();
        let vel_fn = select_velocity(&seg.motion_law);
        let accel_fn = select_acceleration(&seg.motion_law);

        let mut seg_span = seg.end_angle_deg - seg.start_angle_deg;
        if seg_span < 0.0 {
            seg_span += 360.0;
        }
        let seg_span_rad = seg_span.to_radians();
        let h = seg.rise_mm;

        for i in 0..num_points_per_segment {
            let frac = i as f64 / (num_points_per_segment - 1).max(1) as f64;
            let beta = frac;
            let theta_deg = seg.start_angle_deg + frac * seg_span;
            let theta_rad = theta_deg.to_radians();

            let s = disp_fn(beta, h);
            let r = base_radius + cumulative_displacement + s;

            // ds/dθ = (ds/dβ) * (dβ/dθ) = vel(β,h) / seg_span_rad
            let ds_dtheta = if seg_span_rad > 0.0 {
                vel_fn(beta, h) / seg_span_rad
            } else {
                0.0
            };

            // d²s/dθ² for acceleration tracking
            let d2s_dtheta2 = if seg_span_rad > 0.0 {
                accel_fn(beta, h) / (seg_span_rad * seg_span_rad)
            } else {
                0.0
            };
            if d2s_dtheta2.abs() > max_accel {
                max_accel = d2s_dtheta2.abs();
            }

            pitch_points.push((theta_rad, r, ds_dtheta));
            displacement_curve.push([theta_deg, cumulative_displacement + s]);
        }

        cumulative_displacement += h;
    }

    // Remove duplicate last point if it wraps to the first
    if pitch_points.len() > 1 {
        let first_theta = pitch_points[0].0;
        let last_theta = pitch_points.last().unwrap().0;
        if (last_theta - first_theta - 2.0 * PI).abs() < 1e-9
            || (last_theta - first_theta).abs() < 1e-9
        {
            pitch_points.pop();
            displacement_curve.pop();
        }
    }

    // --- Pressure angle computation ---
    let mut max_pressure_angle_deg: f64 = 0.0;
    for &(_, r, ds_dtheta) in &pitch_points {
        if r > 1e-12 {
            let alpha = (ds_dtheta / r).atan().abs();
            let alpha_deg = alpha.to_degrees();
            if alpha_deg > max_pressure_angle_deg {
                max_pressure_angle_deg = alpha_deg;
            }
        }
    }

    // --- Generate cam profile points ---
    let cam_points: Vec<[f64; 2]> = if follower_type == "roller" {
        // Offset the pitch curve outward by -roller_radius along the inward normal.
        // For a roller follower, the actual cam surface is the pitch curve
        // offset inward by roller_radius.
        offset_curve(&pitch_points, -follower_radius, center_x, center_y)
    } else {
        // knife_edge or flat_face: cam surface = pitch curve
        pitch_points
            .iter()
            .map(|&(theta, r, _)| [center_x + r * theta.cos(), center_y + r * theta.sin()])
            .collect()
    };

    // --- Build closed periodic spline ---
    let profile = SketchResult {
        elements: vec![SketchElement::Spline {
            points: cam_points,
            degree: 3,
            periodic: true,
            weights: None,
        }],
        metadata: {
            let mut m = HashMap::new();
            m.insert("base_radius".into(), base_radius);
            m.insert("max_pressure_angle_deg".into(), max_pressure_angle_deg);
            m.insert("max_acceleration".into(), max_accel);
            m.insert("follower_radius".into(), follower_radius);
            m
        },
    };

    Ok(CamResult {
        profile,
        max_pressure_angle_deg,
        max_acceleration: max_accel,
        displacement_curve,
    })
}

/// Offset a polar curve by `offset` along the outward normal.
///
/// For each point, compute the tangent direction, derive the outward normal,
/// and shift the point by `offset` along that normal. Positive offset moves
/// outward (away from center), negative moves inward.
fn offset_curve(
    points: &[(f64, f64, f64)], // (theta, r, ds_dtheta)
    offset: f64,
    cx: f64,
    cy: f64,
) -> Vec<[f64; 2]> {
    let n = points.len();
    if n < 2 {
        return points
            .iter()
            .map(|&(theta, r, _)| [cx + r * theta.cos(), cy + r * theta.sin()])
            .collect();
    }

    // Convert to Cartesian first
    let cart: Vec<[f64; 2]> = points
        .iter()
        .map(|&(theta, r, _)| [cx + r * theta.cos(), cy + r * theta.sin()])
        .collect();

    let mut result = Vec::with_capacity(n);
    for i in 0..n {
        let prev = if i == 0 { n - 1 } else { i - 1 };
        let next = if i == n - 1 { 0 } else { i + 1 };

        let tx = cart[next][0] - cart[prev][0];
        let ty = cart[next][1] - cart[prev][1];
        let tlen = (tx * tx + ty * ty).sqrt();

        if tlen < 1e-12 {
            result.push(cart[i]);
            continue;
        }

        // Outward normal (perpendicular to tangent, pointing away from center)
        let nx = -ty / tlen;
        let ny = tx / tlen;

        // Ensure normal points outward (away from center)
        let dx = cart[i][0] - cx;
        let dy = cart[i][1] - cy;
        let dot = nx * dx + ny * dy;
        let (nx, ny) = if dot < 0.0 { (-nx, -ny) } else { (nx, ny) };

        result.push([cart[i][0] + offset * nx, cart[i][1] + offset * ny]);
    }

    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn simple_rise_dwell_return() -> Vec<CamSegment> {
        vec![
            CamSegment {
                start_angle_deg: 0.0,
                end_angle_deg: 120.0,
                rise_mm: 10.0,
                motion_law: "cycloidal".into(),
            },
            CamSegment {
                start_angle_deg: 120.0,
                end_angle_deg: 180.0,
                rise_mm: 0.0,
                motion_law: "dwell".into(),
            },
            CamSegment {
                start_angle_deg: 180.0,
                end_angle_deg: 300.0,
                rise_mm: -10.0,
                motion_law: "cycloidal".into(),
            },
            CamSegment {
                start_angle_deg: 300.0,
                end_angle_deg: 360.0,
                rise_mm: 0.0,
                motion_law: "dwell".into(),
            },
        ]
    }

    #[test]
    fn test_basic_cam_profile() {
        let segs = simple_rise_dwell_return();
        let result = cam_profile(30.0, &segs, "knife_edge", 0.0, 0.0, 0.0, 36).unwrap();
        // Should produce a closed spline
        assert_eq!(result.profile.elements.len(), 1);
        match &result.profile.elements[0] {
            SketchElement::Spline { periodic, .. } => assert!(*periodic),
            _ => panic!("Expected periodic spline"),
        }
    }

    #[test]
    fn test_displacement_curve_range() {
        let segs = simple_rise_dwell_return();
        let result = cam_profile(30.0, &segs, "knife_edge", 0.0, 0.0, 0.0, 36).unwrap();
        // Displacement should range from ~0 to ~10
        let min_d = result
            .displacement_curve
            .iter()
            .map(|p| p[1])
            .fold(f64::INFINITY, f64::min);
        let max_d = result
            .displacement_curve
            .iter()
            .map(|p| p[1])
            .fold(f64::NEG_INFINITY, f64::max);
        assert!(min_d >= -0.1, "min displacement = {}", min_d);
        assert!((max_d - 10.0).abs() < 0.5, "max displacement = {}", max_d);
    }

    #[test]
    fn test_max_pressure_angle_reasonable() {
        let segs = simple_rise_dwell_return();
        let result = cam_profile(30.0, &segs, "knife_edge", 0.0, 0.0, 0.0, 36).unwrap();
        // For a well-designed cam with base_radius=30 and rise=10, pressure angle
        // should be well under 30 degrees
        assert!(
            result.max_pressure_angle_deg < 30.0,
            "max pressure angle = {:.1} deg",
            result.max_pressure_angle_deg
        );
    }

    #[test]
    fn test_cycloidal_zero_accel_endpoints() {
        // Cycloidal motion: acceleration = h * 2π * sin(2πβ)
        // At β=0 and β=1: sin(0) = sin(2π) = 0
        let a0 = accel_cycloidal(0.0, 10.0);
        let a1 = accel_cycloidal(1.0, 10.0);
        assert!(
            a0.abs() < 1e-10,
            "cycloidal accel at β=0 = {}",
            a0
        );
        assert!(
            a1.abs() < 1e-10,
            "cycloidal accel at β=1 = {}",
            a1
        );
    }

    #[test]
    fn test_polynomial345_boundary_conditions() {
        // 3-4-5 polynomial: s(0)=0, s(1)=h, s'(0)=0, s'(1)=0, s''(0)=0, s''(1)=0
        let h = 15.0;
        assert!((motion_polynomial345(0.0, h)).abs() < 1e-10);
        assert!((motion_polynomial345(1.0, h) - h).abs() < 1e-10);
        assert!((velocity_polynomial345(0.0, h)).abs() < 1e-10);
        assert!((velocity_polynomial345(1.0, h)).abs() < 1e-10);
        assert!((accel_polynomial345(0.0, h)).abs() < 1e-10);
        assert!((accel_polynomial345(1.0, h)).abs() < 1e-10);
    }

    #[test]
    fn test_polynomial4567_boundary_conditions() {
        // 4-5-6-7 polynomial: s(0)=0, s(1)=h, plus zero velocity, accel, jerk at ends
        let h = 20.0;
        assert!((motion_polynomial4567(0.0, h)).abs() < 1e-10);
        assert!((motion_polynomial4567(1.0, h) - h).abs() < 1e-10);
        assert!((velocity_polynomial4567(0.0, h)).abs() < 1e-10);
        assert!((velocity_polynomial4567(1.0, h)).abs() < 1e-10);
    }

    #[test]
    fn test_roller_follower() {
        let segs = simple_rise_dwell_return();
        let result = cam_profile(30.0, &segs, "roller", 5.0, 0.0, 0.0, 36).unwrap();
        assert_eq!(result.profile.elements.len(), 1);
        // Roller offset should produce different points than knife_edge
        let knife = cam_profile(30.0, &segs, "knife_edge", 0.0, 0.0, 0.0, 36).unwrap();
        match (&result.profile.elements[0], &knife.profile.elements[0]) {
            (
                SketchElement::Spline { points: pts_r, .. },
                SketchElement::Spline { points: pts_k, .. },
            ) => {
                // Points should differ
                let diff: f64 = pts_r
                    .iter()
                    .zip(pts_k.iter())
                    .map(|(a, b)| (a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2))
                    .sum();
                assert!(diff > 1.0, "Roller and knife profiles should differ");
            }
            _ => panic!("Expected splines"),
        }
    }

    #[test]
    fn test_segments_not_360_rejected() {
        let segs = vec![CamSegment {
            start_angle_deg: 0.0,
            end_angle_deg: 180.0,
            rise_mm: 10.0,
            motion_law: "cycloidal".into(),
        }];
        let result = cam_profile(30.0, &segs, "knife_edge", 0.0, 0.0, 0.0, 36);
        assert!(result.is_err());
    }

    #[test]
    fn test_invalid_motion_law_rejected() {
        let segs = vec![CamSegment {
            start_angle_deg: 0.0,
            end_angle_deg: 360.0,
            rise_mm: 0.0,
            motion_law: "invalid_law".into(),
        }];
        let result = cam_profile(30.0, &segs, "knife_edge", 0.0, 0.0, 0.0, 36);
        assert!(result.is_err());
    }

    #[test]
    fn test_negative_base_radius_rejected() {
        let segs = vec![CamSegment {
            start_angle_deg: 0.0,
            end_angle_deg: 360.0,
            rise_mm: 0.0,
            motion_law: "dwell".into(),
        }];
        let result = cam_profile(-5.0, &segs, "knife_edge", 0.0, 0.0, 0.0, 36);
        assert!(result.is_err());
    }

    #[test]
    fn test_roller_zero_radius_rejected() {
        let segs = vec![CamSegment {
            start_angle_deg: 0.0,
            end_angle_deg: 360.0,
            rise_mm: 0.0,
            motion_law: "dwell".into(),
        }];
        let result = cam_profile(30.0, &segs, "roller", 0.0, 0.0, 0.0, 36);
        assert!(result.is_err());
    }

    #[test]
    fn test_center_offset() {
        let segs = vec![CamSegment {
            start_angle_deg: 0.0,
            end_angle_deg: 360.0,
            rise_mm: 0.0,
            motion_law: "dwell".into(),
        }];
        let result = cam_profile(20.0, &segs, "knife_edge", 0.0, 10.0, 20.0, 36).unwrap();
        match &result.profile.elements[0] {
            SketchElement::Spline { points, .. } => {
                // All points should be on a circle of radius 20, centered at (10, 20)
                for pt in points {
                    let r = ((pt[0] - 10.0).powi(2) + (pt[1] - 20.0).powi(2)).sqrt();
                    assert!(
                        (r - 20.0).abs() < 0.1,
                        "Point ({:.2}, {:.2}) has radius {:.2} from center",
                        pt[0],
                        pt[1],
                        r
                    );
                }
            }
            _ => panic!("Expected spline"),
        }
    }

    #[test]
    fn test_simple_harmonic_endpoints() {
        let h = 12.0;
        assert!((motion_simple_harmonic(0.0, h)).abs() < 1e-10);
        assert!((motion_simple_harmonic(1.0, h) - h).abs() < 1e-10);
    }

    #[test]
    fn test_dwell_always_zero() {
        for beta in [0.0, 0.25, 0.5, 0.75, 1.0] {
            assert!((motion_dwell(beta, 10.0)).abs() < 1e-10);
        }
    }

    #[test]
    fn test_constant_velocity_linear() {
        let h = 8.0;
        assert!((motion_constant_velocity(0.0, h)).abs() < 1e-10);
        assert!((motion_constant_velocity(0.5, h) - 4.0).abs() < 1e-10);
        assert!((motion_constant_velocity(1.0, h) - 8.0).abs() < 1e-10);
    }

    #[test]
    fn test_all_motion_laws_produce_profile() {
        let laws = [
            "simple_harmonic",
            "cycloidal",
            "polynomial345",
            "polynomial4567",
            "constant_velocity",
        ];
        for law in &laws {
            let segs = vec![
                CamSegment {
                    start_angle_deg: 0.0,
                    end_angle_deg: 180.0,
                    rise_mm: 10.0,
                    motion_law: law.to_string(),
                },
                CamSegment {
                    start_angle_deg: 180.0,
                    end_angle_deg: 360.0,
                    rise_mm: -10.0,
                    motion_law: law.to_string(),
                },
            ];
            let result = cam_profile(25.0, &segs, "knife_edge", 0.0, 0.0, 0.0, 20);
            assert!(
                result.is_ok(),
                "Failed for motion law '{}'",
                law
            );
        }
    }
}
