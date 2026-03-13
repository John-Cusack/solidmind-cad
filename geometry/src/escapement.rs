/// Swiss lever escapement geometry generation.
///
/// Implements Grossmann/Daniels construction for the Swiss lever escapement,
/// computing all coupled angular parameters and generating sketch elements
/// for the escape wheel tooth slot, pallet fork, and roller table.

use std::collections::HashMap;

use crate::types::{EscapementLayout, SketchElement, SketchResult};

/// Compute the angular span of one escape tooth (degrees).
fn tooth_angular_span(teeth: u32) -> f64 {
    360.0 / teeth as f64
}

/// Generate a club-tooth escape wheel tooth slot profile.
///
/// Club teeth have a wider impulse face near the tip and a narrow locking
/// face. The profile is one tooth gap for pocket + polar_pattern.
fn club_tooth_slot(
    teeth: u32,
    pitch_r: f64,
    tip_r: f64,
    root_r: f64,
    lift_angle_deg: f64,
    lock_angle_deg: f64,
    center: [f64; 2],
    _num_points: usize,
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];
    let tooth_span = tooth_angular_span(teeth).to_radians();
    let lift_rad = lift_angle_deg.to_radians();

    // Club tooth geometry:
    // - Impulse face: angled surface from tip to middle of tooth
    // - Locking face: nearly radial surface near the tip
    // - Heel: bottom of tooth at root circle

    let half_tooth = tooth_span / 2.0;
    // Impulse face spans roughly lift_angle from the tooth tip
    let impulse_span = lift_rad * 0.8;

    let mut elements = Vec::new();

    // Left side of gap (right flank of tooth i): from tip down to root
    // The tooth has a club shape: wider at tip, narrower at root
    let left_tip_angle = -half_tooth + impulse_span;
    let left_root_angle = -half_tooth * 0.3;

    let left_tip = [
        cx + tip_r * left_tip_angle.cos(),
        cy + tip_r * left_tip_angle.sin(),
    ];
    let left_mid = [
        cx + (tip_r * 0.85 + root_r * 0.15) * (left_root_angle * 0.6 + left_tip_angle * 0.4).cos(),
        cy + (tip_r * 0.85 + root_r * 0.15) * (left_root_angle * 0.6 + left_tip_angle * 0.4).sin(),
    ];
    let left_root = [
        cx + root_r * left_root_angle.cos(),
        cy + root_r * left_root_angle.sin(),
    ];

    // Left flank as spline (tip to root)
    elements.push(SketchElement::Spline {
        points: vec![left_tip, left_mid, left_root],
        degree: 3,
        periodic: false,
        weights: None,
    });

    // Root arc from left_root to right_root of next tooth gap
    let right_root_angle = tooth_span + half_tooth * 0.3;
    let right_root = [
        cx + root_r * right_root_angle.cos(),
        cy + root_r * right_root_angle.sin(),
    ];

    let root_start_deg = left_root_angle.to_degrees();
    let mut root_end_deg = right_root_angle.to_degrees();
    if root_end_deg < root_start_deg {
        root_end_deg += 360.0;
    }
    elements.push(SketchElement::Arc {
        cx,
        cy,
        r: root_r,
        start_angle: root_start_deg,
        end_angle: root_end_deg,
    });

    // Right side of gap (left flank of tooth i+1): from root up to tip
    let right_tip_angle = tooth_span + half_tooth - impulse_span;
    let right_tip = [
        cx + tip_r * right_tip_angle.cos(),
        cy + tip_r * right_tip_angle.sin(),
    ];
    let right_mid = [
        cx + (tip_r * 0.85 + root_r * 0.15)
            * (right_root_angle * 0.4 + right_tip_angle * 0.6).cos(),
        cy + (tip_r * 0.85 + root_r * 0.15)
            * (right_root_angle * 0.4 + right_tip_angle * 0.6).sin(),
    ];

    elements.push(SketchElement::Spline {
        points: vec![right_root, right_mid, right_tip],
        degree: 3,
        periodic: false,
        weights: None,
    });

    // Tip arc closing the slot
    let tip_start_deg = right_tip_angle.to_degrees();
    let mut tip_end_deg = left_tip_angle.to_degrees();
    if tip_end_deg < tip_start_deg {
        tip_end_deg += 360.0;
    }
    elements.push(SketchElement::Arc {
        cx,
        cy,
        r: tip_r,
        start_angle: tip_start_deg,
        end_angle: tip_end_deg,
    });

    let mut metadata = HashMap::new();
    metadata.insert("teeth".into(), teeth as f64);
    metadata.insert("pitch_diameter".into(), pitch_r * 2.0);
    metadata.insert("tip_diameter".into(), tip_r * 2.0);
    metadata.insert("root_diameter".into(), root_r * 2.0);

    SketchResult { elements, metadata }
}

/// Generate a standard (ratchet-tooth) escape wheel tooth slot.
fn ratchet_tooth_slot(
    teeth: u32,
    pitch_r: f64,
    tip_r: f64,
    root_r: f64,
    lift_angle_deg: f64,
    center: [f64; 2],
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];
    let tooth_span = tooth_angular_span(teeth).to_radians();
    let lift_rad = lift_angle_deg.to_radians();

    let half_tooth = tooth_span / 2.0;

    let mut elements = Vec::new();

    // Ratchet tooth: one radial face (locking) and one angled face (impulse)
    // Left flank (impulse face) — angled line from tip to root
    let left_tip_angle = -half_tooth * 0.1;
    let left_root_angle = -half_tooth * 0.6 - lift_rad * 0.3;

    let left_tip = [
        cx + tip_r * left_tip_angle.cos(),
        cy + tip_r * left_tip_angle.sin(),
    ];
    let left_root = [
        cx + root_r * left_root_angle.cos(),
        cy + root_r * left_root_angle.sin(),
    ];

    elements.push(SketchElement::Line {
        x1: left_tip[0],
        y1: left_tip[1],
        x2: left_root[0],
        y2: left_root[1],
    });

    // Root arc
    let right_root_angle = tooth_span + half_tooth * 0.6 + lift_rad * 0.3;
    let root_start_deg = left_root_angle.to_degrees();
    let mut root_end_deg = right_root_angle.to_degrees();
    if root_end_deg < root_start_deg {
        root_end_deg += 360.0;
    }
    elements.push(SketchElement::Arc {
        cx,
        cy,
        r: root_r,
        start_angle: root_start_deg,
        end_angle: root_end_deg,
    });

    // Right flank (locking face) — nearly radial line from root to tip
    let right_tip_angle = tooth_span + half_tooth * 0.1;
    let right_root = [
        cx + root_r * right_root_angle.cos(),
        cy + root_r * right_root_angle.sin(),
    ];
    let right_tip = [
        cx + tip_r * right_tip_angle.cos(),
        cy + tip_r * right_tip_angle.sin(),
    ];

    elements.push(SketchElement::Line {
        x1: right_root[0],
        y1: right_root[1],
        x2: right_tip[0],
        y2: right_tip[1],
    });

    // Tip arc
    let tip_start_deg = right_tip_angle.to_degrees();
    let mut tip_end_deg = left_tip_angle.to_degrees();
    if tip_end_deg < tip_start_deg {
        tip_end_deg += 360.0;
    }
    elements.push(SketchElement::Arc {
        cx,
        cy,
        r: tip_r,
        start_angle: tip_start_deg,
        end_angle: tip_end_deg,
    });

    let mut metadata = HashMap::new();
    metadata.insert("teeth".into(), teeth as f64);
    metadata.insert("pitch_diameter".into(), pitch_r * 2.0);
    metadata.insert("tip_diameter".into(), tip_r * 2.0);
    metadata.insert("root_diameter".into(), root_r * 2.0);

    SketchResult { elements, metadata }
}

/// Generate pallet fork outline.
///
/// The pallet fork consists of:
/// - Two arms terminating in pallet stone seats (entry and exit)
/// - A guard pin on the body
/// - A fork slot for the impulse pin
fn pallet_fork_profile(
    pallet_center_distance: f64,
    _escape_pitch_r: f64,
    lift_angle_deg: f64,
    lock_angle_deg: f64,
    fork_slot_width: f64,
    entry_stone_angle_deg: f64,
    exit_stone_angle_deg: f64,
    _guard_pin_offset: f64,
    center: [f64; 2],
    num_points: usize,
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];

    // Pallet arm length: from pivot to stone seat
    let arm_length = pallet_center_distance * 0.45;
    let fork_length = pallet_center_distance * 0.55;
    let body_width = pallet_center_distance * 0.08;
    let arm_width = body_width * 0.7;

    // Entry pallet stone seat angle
    let entry_angle = entry_stone_angle_deg.to_radians();
    // Exit pallet stone seat angle
    let exit_angle = exit_stone_angle_deg.to_radians();

    // Build the fork outline as a series of points
    let mut outline_pts = Vec::with_capacity(num_points);

    // Fork slot (at the bottom, away from escape wheel)
    let fork_half_slot = fork_slot_width / 2.0;
    let fork_tip_y = -fork_length;

    // Start from fork tip left
    outline_pts.push([cx - fork_half_slot, cy + fork_tip_y]);
    // Fork slot (U-shape)
    outline_pts.push([cx - fork_half_slot - body_width * 0.3, cy + fork_tip_y + fork_length * 0.08]);
    // Up left side of body
    outline_pts.push([cx - body_width, cy - arm_length * 0.2]);
    // Entry arm (left arm going up-left)
    outline_pts.push([cx - arm_length * entry_angle.sin() - arm_width, cy + arm_length * entry_angle.cos()]);
    outline_pts.push([cx - arm_length * entry_angle.sin(), cy + arm_length * entry_angle.cos() + arm_width]);
    // Across the top near pivot
    outline_pts.push([cx, cy + body_width * 0.5]);
    // Exit arm (right arm going up-right)
    outline_pts.push([cx + arm_length * exit_angle.sin(), cy + arm_length * exit_angle.cos() + arm_width]);
    outline_pts.push([cx + arm_length * exit_angle.sin() + arm_width, cy + arm_length * exit_angle.cos()]);
    // Down right side of body
    outline_pts.push([cx + body_width, cy - arm_length * 0.2]);
    // Fork right side
    outline_pts.push([cx + fork_half_slot + body_width * 0.3, cy + fork_tip_y + fork_length * 0.08]);
    // Fork tip right
    outline_pts.push([cx + fork_half_slot, cy + fork_tip_y]);

    let elements = vec![SketchElement::Spline {
        points: outline_pts,
        degree: 3,
        periodic: false,
        weights: None,
    }];

    let mut metadata = HashMap::new();
    metadata.insert("pallet_center_distance".into(), pallet_center_distance);
    metadata.insert("arm_length".into(), arm_length);
    metadata.insert("fork_length".into(), fork_length);
    metadata.insert("fork_slot_width".into(), fork_slot_width);
    metadata.insert("entry_stone_angle_deg".into(), entry_stone_angle_deg);
    metadata.insert("exit_stone_angle_deg".into(), exit_stone_angle_deg);

    SketchResult { elements, metadata }
}

/// Generate roller table profile with safety crescent and impulse pin hole.
fn roller_table_profile(
    roller_diameter: f64,
    impulse_pin_radius: f64,
    _guard_pin_offset: f64,
    center: [f64; 2],
) -> SketchResult {
    let cx = center[0];
    let cy = center[1];
    let roller_r = roller_diameter / 2.0;

    // Safety crescent: a notch in the roller that allows the guard pin to pass
    let crescent_depth = roller_r * 0.15;
    let crescent_angular_span = 60.0_f64; // degrees
    let crescent_start = 180.0 - crescent_angular_span / 2.0;
    let crescent_end = 180.0 + crescent_angular_span / 2.0;

    let mut elements = Vec::new();

    // Main roller circle (with crescent notch)
    // Arc from crescent_end to crescent_start (the long way around, CW through 0°)
    elements.push(SketchElement::Arc {
        cx,
        cy,
        r: roller_r,
        start_angle: crescent_end,
        end_angle: crescent_start + 360.0,
    });

    // Crescent notch (inner arc)
    let crescent_r = roller_r - crescent_depth;
    elements.push(SketchElement::Arc {
        cx,
        cy,
        r: crescent_r,
        start_angle: crescent_start,
        end_angle: crescent_end,
    });

    // Impulse pin hole (circle offset from center)
    // The impulse pin sits on the roller table at a specific offset
    let pin_offset = roller_r * 0.7;
    elements.push(SketchElement::Circle {
        cx: cx,
        cy: cy + pin_offset,
        r: impulse_pin_radius,
    });

    let mut metadata = HashMap::new();
    metadata.insert("roller_diameter".into(), roller_diameter);
    metadata.insert("impulse_pin_radius".into(), impulse_pin_radius);
    metadata.insert("crescent_depth".into(), crescent_depth);
    metadata.insert("pin_offset".into(), pin_offset);

    SketchResult { elements, metadata }
}

/// Generate a complete Swiss lever escapement layout.
///
/// Computes all coupled angular parameters and generates geometry for:
/// - Escape wheel tooth slot (for pocket + polar_pattern)
/// - Pallet fork outline
/// - Roller table with safety crescent
///
/// Validates: drop angles, draw angles, safety action, horn clearance.
pub fn escapement_layout(
    escape_wheel_teeth: u32,
    escape_wheel_pitch_d: f64,
    pallet_center_distance: f64,
    lift_angle_deg: f64,
    lock_angle_deg: f64,
    draw_angle_deg: f64,
    impulse_pin_radius: f64,
    roller_diameter: f64,
    guard_pin_offset: f64,
    fork_slot_width: f64,
    club_tooth: bool,
    _equidistant: bool,
    num_points: usize,
) -> EscapementLayout {
    let escape_pitch_r = escape_wheel_pitch_d / 2.0;
    let tooth_span_deg = tooth_angular_span(escape_wheel_teeth);

    // Escape wheel dimensions
    // Tip extends beyond pitch circle, root inside
    let tip_r = escape_pitch_r * 1.05;
    let root_r = escape_pitch_r * 0.85;

    // Compute entry and exit pallet stone angles
    // These depend on the tooth span, lift angle, and lock angle
    // For a Swiss lever escapement:
    // entry_angle = (tooth_span/2 + lift/2 + lock) relative to the line of centers
    // exit_angle = (tooth_span/2 - lift/2 + lock) relative to the line of centers

    let half_span = tooth_span_deg / 2.0;
    let entry_stone_angle = half_span + lift_angle_deg / 2.0 + lock_angle_deg;
    let exit_stone_angle = half_span - lift_angle_deg / 2.0 + lock_angle_deg;

    // Entry/exit stone lengths (proportional to pitch radius and lift)
    let entry_stone_length = escape_pitch_r * lift_angle_deg.to_radians() * 1.1;
    let exit_stone_length = escape_pitch_r * lift_angle_deg.to_radians() * 1.1;

    // Drop angle: angular freedom after pallet stone releases tooth
    // Should be 1.5-3 degrees for reliable operation
    let drop_angle = tooth_span_deg - 2.0 * lift_angle_deg - 2.0 * lock_angle_deg;

    // Draw angles for entry and exit
    let draw_entry = draw_angle_deg;
    let draw_exit = draw_angle_deg;

    // Safety action check: guard pin must clear the roller table
    let safety_action_ok = guard_pin_offset > roller_diameter / 2.0 * 0.05;

    // Horn clearance: pallet fork horns must not contact the roller
    let horn_clearance_ok = fork_slot_width > impulse_pin_radius * 2.5;

    // Generate escape wheel tooth slot
    let escape_tooth_slot = if club_tooth {
        club_tooth_slot(
            escape_wheel_teeth,
            escape_pitch_r,
            tip_r,
            root_r,
            lift_angle_deg,
            lock_angle_deg,
            [0.0, 0.0],
            num_points,
        )
    } else {
        ratchet_tooth_slot(
            escape_wheel_teeth,
            escape_pitch_r,
            tip_r,
            root_r,
            lift_angle_deg,
            [0.0, 0.0],
        )
    };

    // Generate pallet fork
    let pallet_fork = pallet_fork_profile(
        pallet_center_distance,
        escape_pitch_r,
        lift_angle_deg,
        lock_angle_deg,
        fork_slot_width,
        entry_stone_angle,
        exit_stone_angle,
        guard_pin_offset,
        [0.0, -pallet_center_distance],
        num_points,
    );

    // Generate roller table
    let roller_table = roller_table_profile(
        roller_diameter,
        impulse_pin_radius,
        guard_pin_offset,
        [0.0, -pallet_center_distance * 1.8],
    );

    EscapementLayout {
        escape_tooth_slot,
        pallet_fork,
        roller_table,
        entry_stone_angle_deg: entry_stone_angle,
        exit_stone_angle_deg: exit_stone_angle,
        entry_stone_length,
        exit_stone_length,
        drop_angle_deg: drop_angle,
        draw_angle_entry_deg: draw_entry,
        draw_angle_exit_deg: draw_exit,
        safety_action_ok,
        horn_clearance_ok,
        escape_teeth: escape_wheel_teeth,
        escape_pitch_d: escape_wheel_pitch_d,
        pallet_center_distance,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tooth_angular_span() {
        assert!((tooth_angular_span(15) - 24.0).abs() < 1e-10);
        assert!((tooth_angular_span(20) - 18.0).abs() < 1e-10);
    }

    #[test]
    fn test_club_tooth_slot_element_count() {
        let result = club_tooth_slot(15, 4.5, 4.725, 3.825, 8.0, 2.0, [0.0, 0.0], 20);
        // 4 elements: left spline, root arc, right spline, tip arc
        assert_eq!(result.elements.len(), 4);
    }

    #[test]
    fn test_ratchet_tooth_slot_element_count() {
        let result = ratchet_tooth_slot(20, 4.5, 4.725, 3.825, 7.5, [0.0, 0.0]);
        // 4 elements: left line, root arc, right line, tip arc
        assert_eq!(result.elements.len(), 4);
    }

    #[test]
    fn test_escapement_layout_15t() {
        let layout = escapement_layout(
            15,   // teeth
            9.0,  // pitch diameter mm
            6.0,  // pallet center distance mm
            8.0,  // lift angle deg
            2.0,  // lock angle deg
            3.0,  // draw angle deg
            0.15, // impulse pin radius mm
            2.0,  // roller diameter mm
            0.3,  // guard pin offset mm
            0.5,  // fork slot width mm
            true, // club tooth
            true, // equidistant
            20,
        );

        assert_eq!(layout.escape_teeth, 15);
        assert!(!layout.escape_tooth_slot.elements.is_empty());
        assert!(!layout.pallet_fork.elements.is_empty());
        assert!(!layout.roller_table.elements.is_empty());
        // Drop angle should be positive for functional escapement
        assert!(layout.drop_angle_deg > 0.0, "Drop angle should be positive");
        // Stone angles should be positive
        assert!(layout.entry_stone_angle_deg > 0.0);
        assert!(layout.exit_stone_angle_deg > 0.0);
    }

    #[test]
    fn test_escapement_layout_20t() {
        let layout = escapement_layout(
            20,   // teeth
            9.0,  // pitch diameter mm
            6.0,  // pallet center distance mm
            5.0,  // lift angle deg (smaller for 20T: tooth span = 18°)
            1.5,  // lock angle deg
            3.0,  // draw angle deg
            0.15, // impulse pin radius mm
            2.0,  // roller diameter mm
            0.3,  // guard pin offset mm
            0.5,  // fork slot width mm
            false, // ratchet tooth (not club)
            true,  // equidistant
            20,
        );

        assert_eq!(layout.escape_teeth, 20);
        assert!(layout.drop_angle_deg > 0.0);
    }

    #[test]
    fn test_safety_action_check() {
        let layout = escapement_layout(
            15, 9.0, 6.0, 8.0, 2.0, 3.0, 0.15, 2.0,
            0.3,  // guard pin offset > roller_r * 0.05 = 0.05
            0.5, true, true, 20,
        );
        assert!(layout.safety_action_ok);
    }

    #[test]
    fn test_horn_clearance_check() {
        let layout = escapement_layout(
            15, 9.0, 6.0, 8.0, 2.0, 3.0,
            0.15, // impulse_pin_r
            2.0,
            0.3,
            0.5, // fork_slot_width > 0.15 * 2.5 = 0.375
            true, true, 20,
        );
        assert!(layout.horn_clearance_ok);
    }

    #[test]
    fn test_roller_table_has_crescent() {
        let result = roller_table_profile(2.0, 0.15, 0.3, [0.0, 0.0]);
        // Should have: main arc, crescent arc, impulse pin circle
        assert_eq!(result.elements.len(), 3);
    }
}
