/// Planetary (epicyclic) gear set layout calculator.
///
/// Validates gear tooth counts, computes planet positions, and generates
/// all three gear profiles (sun, planet, ring).

use std::f64::consts::PI;

use crate::gears::{
    compute_gear_params, compute_internal_gear_params, internal_gear_profile,
    single_internal_tooth_slot, spur_gear_profile,
};
use crate::types::{GearParams, PlanetaryLayout, SketchElement, SketchResult};

/// Error type for planetary layout validation.
#[derive(Debug)]
pub enum PlanetaryError {
    /// Ring teeth must equal sun + 2*planet
    InvalidRingTeeth {
        sun: u32,
        planet: u32,
        expected_ring: u32,
    },
    /// (sun + ring) must be divisible by num_planets for even spacing
    AssemblyCondition {
        sun: u32,
        ring: u32,
        num_planets: u32,
    },
    /// Need at least 2 planets
    TooFewPlanets(u32),
}

impl std::fmt::Display for PlanetaryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PlanetaryError::InvalidRingTeeth {
                sun,
                planet,
                expected_ring,
            } => write!(
                f,
                "Ring teeth must be sun + 2*planet = {} + 2*{} = {}",
                sun, planet, expected_ring
            ),
            PlanetaryError::AssemblyCondition {
                sun,
                ring,
                num_planets,
            } => write!(
                f,
                "Assembly condition failed: (sun + ring) = ({} + {}) = {} must be divisible by num_planets = {}",
                sun, ring, sun + ring, num_planets
            ),
            PlanetaryError::TooFewPlanets(n) => {
                write!(f, "Need at least 2 planets, got {}", n)
            }
        }
    }
}

/// Compute the planet center positions for a planetary gear set.
///
/// Planets are evenly spaced on a circle of radius (sun_pitch_r + planet_pitch_r).
pub fn planet_positions(
    sun_params: &GearParams,
    planet_params: &GearParams,
    num_planets: u32,
    center: [f64; 2],
) -> Vec<[f64; 2]> {
    let orbit_r = (sun_params.pitch_diameter + planet_params.pitch_diameter) / 2.0;
    let angle_step = 2.0 * PI / num_planets as f64;

    (0..num_planets)
        .map(|i| {
            let angle = i as f64 * angle_step;
            [
                center[0] + orbit_r * angle.cos(),
                center[1] + orbit_r * angle.sin(),
            ]
        })
        .collect()
}

/// Generate a complete planetary gear layout.
///
/// Validates tooth counts, computes parameters, generates all profiles.
pub fn planetary_layout(
    module: f64,
    sun_teeth: u32,
    planet_teeth: u32,
    num_planets: u32,
    pressure_angle_deg: f64,
    clearance_coeff: f64,
    profile_shift: f64,
    backlash: f64,
    center: [f64; 2],
    num_involute_pts: usize,
) -> Result<PlanetaryLayout, PlanetaryError> {
    if num_planets < 2 {
        return Err(PlanetaryError::TooFewPlanets(num_planets));
    }

    let ring_teeth = sun_teeth + 2 * planet_teeth;

    // Assembly condition: (sun + ring) must be divisible by num_planets
    if (sun_teeth + ring_teeth) % num_planets != 0 {
        return Err(PlanetaryError::AssemblyCondition {
            sun: sun_teeth,
            ring: ring_teeth,
            num_planets,
        });
    }

    let sun_params = compute_gear_params(
        module,
        sun_teeth,
        pressure_angle_deg,
        clearance_coeff,
        profile_shift,
        backlash,
    );
    let planet_params = compute_gear_params(
        module,
        planet_teeth,
        pressure_angle_deg,
        clearance_coeff,
        profile_shift,
        backlash,
    );
    let ring_params = compute_internal_gear_params(
        module,
        ring_teeth,
        pressure_angle_deg,
        clearance_coeff,
        profile_shift,
        backlash,
    );

    // Generate sun gear at center
    let sun = spur_gear_profile(&sun_params, center, num_involute_pts);

    // Generate planet gear at first planet position (user can place others)
    let positions = planet_positions(&sun_params, &planet_params, num_planets, center);
    let planet = spur_gear_profile(&planet_params, positions[0], num_involute_pts);

    // Generate ring gear at center
    let ring = internal_gear_profile(&ring_params, center, num_involute_pts);

    // Ring blank: circle at rf + 1.5*module (standard ring gear wall thickness)
    let ring_rf = ring_params.root_diameter / 2.0;
    let ring_blank_r = ring_rf + 1.5 * module;
    let ring_blank = SketchResult {
        elements: vec![SketchElement::Circle {
            cx: center[0],
            cy: center[1],
            r: ring_blank_r,
        }],
        metadata: {
            let mut m = ring_params.to_metadata();
            m.insert("blank_outer_radius".into(), ring_blank_r);
            m
        },
    };

    // Ring tooth slot: single internal tooth slot for pocket + polar pattern
    let ring_tooth_slot = single_internal_tooth_slot(&ring_params, center, num_involute_pts);

    Ok(PlanetaryLayout {
        sun,
        planet,
        ring,
        ring_blank,
        ring_tooth_slot,
        planet_positions: positions,
        sun_params,
        planet_params,
        ring_params,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ring_teeth_formula() {
        // ring = sun + 2*planet
        let sun = 18;
        let planet = 9;
        let ring = sun + 2 * planet;
        assert_eq!(ring, 36);
    }

    #[test]
    fn test_assembly_condition_pass() {
        // sun=18, planet=9, ring=36, num_planets=3
        // (18+36) % 3 = 54 % 3 = 0 ✓
        let result = planetary_layout(1.0, 18, 9, 3, 20.0, 0.25, 0.0, 0.0, [0.0, 0.0], 20);
        assert!(result.is_ok());
    }

    #[test]
    fn test_assembly_condition_fail() {
        // sun=18, planet=10, ring=38
        // (18+38) % 3 = 56 % 3 = 2 ✗
        let result = planetary_layout(1.0, 18, 10, 3, 20.0, 0.25, 0.0, 0.0, [0.0, 0.0], 20);
        assert!(matches!(result, Err(PlanetaryError::AssemblyCondition { .. })));
    }

    #[test]
    fn test_too_few_planets() {
        let result = planetary_layout(1.0, 18, 9, 1, 20.0, 0.25, 0.0, 0.0, [0.0, 0.0], 20);
        assert!(matches!(result, Err(PlanetaryError::TooFewPlanets(1))));
    }

    #[test]
    fn test_planet_positions_count() {
        let sun_p = compute_gear_params(1.0, 18, 20.0, 0.25, 0.0, 0.0);
        let planet_p = compute_gear_params(1.0, 9, 20.0, 0.25, 0.0, 0.0);
        let positions = planet_positions(&sun_p, &planet_p, 3, [0.0, 0.0]);
        assert_eq!(positions.len(), 3);
    }

    #[test]
    fn test_planet_positions_equidistant() {
        let sun_p = compute_gear_params(1.0, 18, 20.0, 0.25, 0.0, 0.0);
        let planet_p = compute_gear_params(1.0, 9, 20.0, 0.25, 0.0, 0.0);
        let positions = planet_positions(&sun_p, &planet_p, 4, [0.0, 0.0]);

        // All planets should be at the same distance from center
        let r0 = (positions[0][0].powi(2) + positions[0][1].powi(2)).sqrt();
        for pos in &positions[1..] {
            let r = (pos[0].powi(2) + pos[1].powi(2)).sqrt();
            assert!(
                (r - r0).abs() < 1e-10,
                "planets should be equidistant from center"
            );
        }

        // Angular spacing should be equal
        let expected_spacing = PI / 2.0; // 360° / 4
        for i in 0..4 {
            let j = (i + 1) % 4;
            let angle_i = positions[i][1].atan2(positions[i][0]);
            let angle_j = positions[j][1].atan2(positions[j][0]);
            let mut diff = angle_j - angle_i;
            if diff < 0.0 {
                diff += 2.0 * PI;
            }
            assert!(
                (diff - expected_spacing).abs() < 1e-10,
                "angular spacing should be equal"
            );
        }
    }

    #[test]
    fn test_planet_orbit_radius() {
        let sun_p = compute_gear_params(2.0, 18, 20.0, 0.25, 0.0, 0.0);
        let planet_p = compute_gear_params(2.0, 9, 20.0, 0.25, 0.0, 0.0);
        let positions = planet_positions(&sun_p, &planet_p, 3, [0.0, 0.0]);

        let expected_orbit = (sun_p.pitch_diameter + planet_p.pitch_diameter) / 2.0;
        let r = (positions[0][0].powi(2) + positions[0][1].powi(2)).sqrt();
        assert!((r - expected_orbit).abs() < 1e-10);
    }

    #[test]
    fn test_layout_has_all_profiles() {
        let layout =
            planetary_layout(1.0, 18, 9, 3, 20.0, 0.25, 0.0, 0.0, [0.0, 0.0], 20).unwrap();
        assert!(!layout.sun.elements.is_empty());
        assert!(!layout.planet.elements.is_empty());
        assert!(!layout.ring.elements.is_empty());
        assert_eq!(layout.planet_positions.len(), 3);
    }

    #[test]
    fn test_layout_with_center_offset() {
        let layout =
            planetary_layout(1.0, 18, 9, 3, 20.0, 0.25, 0.0, 0.0, [50.0, 50.0], 20).unwrap();

        // Planet positions should be offset
        for pos in &layout.planet_positions {
            let r_from_center =
                ((pos[0] - 50.0).powi(2) + (pos[1] - 50.0).powi(2)).sqrt();
            assert!(r_from_center > 1.0, "planets should be away from center");
        }
    }

    #[test]
    fn test_layout_ring_blank_radius() {
        let module = 2.0;
        let layout =
            planetary_layout(module, 18, 9, 3, 20.0, 0.25, 0.0, 0.0, [0.0, 0.0], 20).unwrap();

        let ring_rf = layout.ring_params.root_diameter / 2.0;
        let expected_outer = ring_rf + 1.5 * module;

        // The blank should have exactly one Circle element
        assert_eq!(layout.ring_blank.elements.len(), 1);
        match &layout.ring_blank.elements[0] {
            crate::types::SketchElement::Circle { r, .. } => {
                assert!(
                    (r - expected_outer).abs() < 1e-10,
                    "ring blank radius {:.3} != expected {:.3}",
                    r, expected_outer
                );
            }
            _ => panic!("ring_blank should contain a Circle element"),
        }
    }

    #[test]
    fn test_layout_ring_tooth_slot_nonempty() {
        let layout =
            planetary_layout(1.0, 18, 9, 3, 20.0, 0.25, 0.0, 0.0, [0.0, 0.0], 20).unwrap();
        assert!(
            !layout.ring_tooth_slot.elements.is_empty(),
            "ring_tooth_slot should have elements"
        );
        // Should have 4 or 6 elements depending on whether radial lines are needed
        let n = layout.ring_tooth_slot.elements.len();
        assert!(n == 4 || n == 6, "expected 4 or 6 elements, got {}", n);
    }
}
