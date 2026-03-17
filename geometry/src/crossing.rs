/// Spoke pattern geometry generation.
///
/// Generates spoke (crossing) pocket profiles for wheels, pulleys, flywheels,
/// turbine discs, and any hub-and-rim component that needs weight reduction
/// or traditional spoke styling.

use std::collections::HashMap;
use std::f64::consts::PI;

use crate::involute::rotate_point;
use crate::types::{SketchElement, SketchResult};

/// Generate a single spoke profile from hub to rim.
///
/// Returns (right_side, left_side) point arrays in spoke-local coordinates
/// where the spoke runs along +Y from hub_r to rim_inner_r.
fn spoke_profile_points(
    hub_r: f64,
    rim_inner_r: f64,
    spoke_width_hub: f64,
    spoke_width_rim: f64,
    spoke_style: &str,
    num_points: usize,
) -> (Vec<[f64; 2]>, Vec<[f64; 2]>) {
    let num_points = num_points.max(4);

    let mut right_side = Vec::with_capacity(num_points);
    let mut left_side = Vec::with_capacity(num_points);

    for i in 0..num_points {
        let frac = i as f64 / (num_points - 1) as f64;
        let r = hub_r + (rim_inner_r - hub_r) * frac;
        let half_w = (spoke_width_hub + (spoke_width_rim - spoke_width_hub) * frac) / 2.0;

        let lateral_offset = match spoke_style {
            "curved_s" => {
                let amplitude = (rim_inner_r - hub_r) * 0.08;
                amplitude * (frac * PI).sin()
            }
            "curved_c" => {
                let amplitude = (rim_inner_r - hub_r) * 0.06;
                amplitude * (frac * PI / 2.0).sin()
            }
            _ => 0.0, // "straight" and "tapered"
        };

        right_side.push([lateral_offset + half_w, r]);
        left_side.push([lateral_offset - half_w, r]);
    }

    (right_side, left_side)
}

/// Generate one inter-spoke pocket profile for pocket + polar_pattern workflow.
///
/// The pocket is the material between two adjacent spokes — removing it
/// with pocket + polar_pattern creates the spoke pattern.
pub fn spoke_pattern(
    hub_diameter: f64,
    rim_inner_diameter: f64,
    rim_outer_diameter: f64,
    num_spokes: u32,
    spoke_style: &str,
    spoke_width_hub: f64,
    spoke_width_rim: f64,
    _fillet_radius_hub: f64,
    _fillet_radius_rim: f64,
    center_x: f64,
    center_y: f64,
    num_points: usize,
) -> SketchResult {
    let cx = center_x;
    let cy = center_y;
    let hub_r = hub_diameter / 2.0;
    let rim_inner_r = rim_inner_diameter / 2.0;
    let _rim_outer_r = rim_outer_diameter / 2.0;
    let spoke_angle = 2.0 * PI / num_spokes as f64;

    let (right_profile, left_profile) = spoke_profile_points(
        hub_r, rim_inner_r, spoke_width_hub, spoke_width_rim, spoke_style, num_points,
    );

    let mut elements = Vec::new();

    // Right edge of spoke 0, rotated to angle 0
    let right_of_spoke0: Vec<[f64; 2]> = right_profile
        .iter()
        .map(|p| {
            let (rx, ry) = rotate_point(p[0], p[1], 0.0);
            [cx + rx, cy + ry]
        })
        .collect();

    // Left edge of spoke 1, rotated by spoke_angle
    let left_of_spoke1: Vec<[f64; 2]> = left_profile
        .iter()
        .map(|p| {
            let (rx, ry) = rotate_point(p[0], p[1], spoke_angle);
            [cx + rx, cy + ry]
        })
        .collect();

    // Right edge of spoke 0 (hub to rim)
    elements.push(SketchElement::Spline {
        points: right_of_spoke0.clone(),
        degree: 3,
        periodic: false,
        weights: None,
    });

    // Rim inner arc from spoke 0 to spoke 1
    let r0_outer = right_of_spoke0.last().unwrap();
    let r1_outer = left_of_spoke1.last().unwrap();
    let angle_r0 = (r0_outer[1] - cy).atan2(r0_outer[0] - cx);
    let angle_r1 = (r1_outer[1] - cy).atan2(r1_outer[0] - cx);

    let rim_start_deg = angle_r0.to_degrees();
    let mut rim_end_deg = angle_r1.to_degrees();
    if rim_end_deg < rim_start_deg {
        rim_end_deg += 360.0;
    }

    elements.push(SketchElement::Arc {
        cx, cy, r: rim_inner_r,
        start_angle: rim_start_deg,
        end_angle: rim_end_deg,
    });

    // Left edge of spoke 1 (rim to hub, reversed)
    let mut left_of_spoke1_rev = left_of_spoke1;
    left_of_spoke1_rev.reverse();
    elements.push(SketchElement::Spline {
        points: left_of_spoke1_rev.clone(),
        degree: 3,
        periodic: false,
        weights: None,
    });

    // Hub arc back to spoke 0
    let r1_inner = left_of_spoke1_rev.last().unwrap();
    let r0_inner = &right_of_spoke0[0];
    let angle_r1_inner = (r1_inner[1] - cy).atan2(r1_inner[0] - cx);
    let angle_r0_inner = (r0_inner[1] - cy).atan2(r0_inner[0] - cx);

    let hub_start_deg = angle_r1_inner.to_degrees();
    let mut hub_end_deg = angle_r0_inner.to_degrees();
    if hub_end_deg > hub_start_deg {
        hub_end_deg -= 360.0;
    }
    elements.push(SketchElement::Arc {
        cx, cy, r: hub_r,
        start_angle: hub_start_deg,
        end_angle: hub_end_deg + 360.0,
    });

    // Weight reduction estimate
    let avg_r = (hub_r + rim_inner_r) / 2.0;
    let avg_spoke_w = (spoke_width_hub + spoke_width_rim) / 2.0;
    let spoke_angular_width = avg_spoke_w / avg_r;
    let gap_angle = spoke_angle - spoke_angular_width;
    let gap_area = gap_angle * (rim_inner_r * rim_inner_r - hub_r * hub_r) / 2.0;
    let total_annular_area = PI * (rim_inner_r * rim_inner_r - hub_r * hub_r);
    let weight_reduction_pct = if total_annular_area > 0.0 {
        (gap_area * num_spokes as f64 / total_annular_area) * 100.0
    } else {
        0.0
    };

    let mut metadata = HashMap::new();
    metadata.insert("hub_diameter".into(), hub_diameter);
    metadata.insert("rim_inner_diameter".into(), rim_inner_diameter);
    metadata.insert("rim_outer_diameter".into(), rim_outer_diameter);
    metadata.insert("num_spokes".into(), num_spokes as f64);
    metadata.insert("weight_reduction_pct".into(), weight_reduction_pct);

    SketchResult { elements, metadata }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_spoke_profile_straight() {
        let (right, left) = spoke_profile_points(2.0, 8.0, 1.0, 1.0, "straight", 10);
        assert_eq!(right.len(), 10);
        assert_eq!(left.len(), 10);
        for (r, l) in right.iter().zip(left.iter()) {
            assert!((r[0] + l[0]).abs() < 1e-10, "should be symmetric");
        }
    }

    #[test]
    fn test_spoke_profile_tapered() {
        let (right, left) = spoke_profile_points(2.0, 8.0, 1.5, 0.8, "tapered", 10);
        let hub_width = right[0][0] - left[0][0];
        let rim_width = right[9][0] - left[9][0];
        assert!(hub_width > rim_width);
    }

    #[test]
    fn test_spoke_profile_curved_s() {
        let (right, _left) = spoke_profile_points(2.0, 8.0, 1.0, 1.0, "curved_s", 20);
        let mid = right.len() / 2;
        assert!(right[mid][0].abs() > 0.01);
    }

    #[test]
    fn test_spoke_pattern_element_count() {
        let result = spoke_pattern(3.0, 10.0, 12.0, 4, "straight", 0.8, 0.6, 0.1, 0.1, 0.0, 0.0, 10);
        assert_eq!(result.elements.len(), 4);
    }

    #[test]
    fn test_spoke_pattern_metadata() {
        let result = spoke_pattern(3.0, 10.0, 12.0, 5, "tapered", 0.8, 0.5, 0.1, 0.1, 0.0, 0.0, 10);
        assert!(result.metadata.contains_key("weight_reduction_pct"));
        let wr = result.metadata["weight_reduction_pct"];
        assert!(wr > 0.0 && wr < 100.0);
    }

    #[test]
    fn test_spoke_pattern_styles() {
        for style in &["straight", "tapered", "curved_s", "curved_c"] {
            let result = spoke_pattern(3.0, 10.0, 12.0, 4, style, 0.8, 0.6, 0.1, 0.1, 0.0, 0.0, 10);
            assert_eq!(result.elements.len(), 4, "style '{}' should produce 4 elements", style);
        }
    }

    #[test]
    fn test_spoke_pattern_center_offset() {
        let result = spoke_pattern(3.0, 10.0, 12.0, 3, "straight", 0.8, 0.6, 0.1, 0.1, 5.0, 10.0, 10);
        for elem in &result.elements {
            if let SketchElement::Arc { cx, cy, .. } = elem {
                assert!((cx - 5.0).abs() < 1e-10);
                assert!((cy - 10.0).abs() < 1e-10);
            }
        }
    }

    #[test]
    fn test_spoke_pattern_various_counts() {
        for spokes in 2..=12 {
            let result = spoke_pattern(3.0, 10.0, 12.0, spokes, "straight", 0.8, 0.6, 0.1, 0.1, 0.0, 0.0, 10);
            assert_eq!(result.elements.len(), 4, "Expected 4 elements for {} spokes", spokes);
        }
    }
}
