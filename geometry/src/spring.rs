/// Helical spring design — computes spring parameters and returns helix +
/// wire cross-section for CAD building (sweep along helix path).
///
/// Formulas follow Shigley's Mechanical Engineering Design.

use std::collections::HashMap;
use std::f64::consts::PI;

use crate::types::{SketchElement, SketchResult, SpringResult};

/// Compute helical spring parameters and generate wire cross-section sketch.
///
/// # Arguments
/// * `spring_type` — "compression", "extension", or "torsion"
/// * `wire_diameter` — wire diameter d (mm)
/// * `coil_diameter` — mean coil diameter D (mm)
/// * `active_coils` — number of active coils Na
/// * `free_length` — free (unloaded) length (mm, compression only)
/// * `material_g_gpa` — shear modulus G (GPa)
/// * `material_yield_mpa` — optional shear yield strength (MPa)
/// * `end_type` — "closed_ground", "closed", "open", "open_ground"
/// * `design_load` — optional design load F (N)
pub fn helical_spring(
    spring_type: &str,
    wire_diameter: f64,
    coil_diameter: f64,
    active_coils: f64,
    free_length: f64,
    material_g_gpa: f64,
    material_yield_mpa: Option<f64>,
    end_type: &str,
    design_load: Option<f64>,
) -> Result<SpringResult, String> {
    // --- Validation ---
    let valid_types = ["compression", "extension", "torsion"];
    if !valid_types.contains(&spring_type) {
        return Err(format!(
            "spring_type must be one of {:?}, got '{}'",
            valid_types, spring_type
        ));
    }
    let valid_ends = ["closed_ground", "closed", "open", "open_ground"];
    if !valid_ends.contains(&end_type) {
        return Err(format!(
            "end_type must be one of {:?}, got '{}'",
            valid_ends, end_type
        ));
    }
    if wire_diameter <= 0.0 {
        return Err("wire_diameter must be positive".into());
    }
    if coil_diameter <= wire_diameter {
        return Err("coil_diameter must be greater than wire_diameter".into());
    }
    if active_coils < 1.0 {
        return Err("active_coils must be >= 1".into());
    }
    if free_length <= 0.0 && spring_type == "compression" {
        return Err("free_length must be positive for compression springs".into());
    }
    if material_g_gpa <= 0.0 {
        return Err("material_g_gpa must be positive".into());
    }

    let d = wire_diameter;
    let big_d = coil_diameter;
    let na = active_coils;
    let g_pa = material_g_gpa * 1e3; // GPa → MPa

    // --- Spring index & Wahl factor ---
    let c = big_d / d;
    if c < 3.0 {
        return Err(format!(
            "Spring index C = D/d = {:.2} is too small (minimum 3.0)",
            c
        ));
    }
    let kw = (4.0 * c - 1.0) / (4.0 * c - 4.0) + 0.615 / c;

    // --- Spring rate ---
    // k = G * d^4 / (8 * D^3 * Na)  [N/mm when G in MPa, d/D in mm]
    let k = g_pa * d.powi(4) / (8.0 * big_d.powi(3) * na);

    // --- Total coils & solid height ---
    let total_coils = match end_type {
        "closed_ground" => na + 2.0,
        "closed" => na + 3.0,
        "open" => na + 1.0,
        "open_ground" => na + 2.0,
        _ => na + 2.0,
    };

    let solid_height = match end_type {
        "closed_ground" => (na + 2.0) * d,
        "closed" => (na + 3.0) * d,
        "open" => (na + 1.0) * d,
        "open_ground" => (na + 2.0) * d,
        _ => (na + 2.0) * d,
    };

    // --- Max deflection ---
    let max_deflection = if spring_type == "compression" {
        free_length - solid_height
    } else {
        // For extension/torsion, max deflection is less meaningful without
        // free_length context; report based on a nominal range.
        free_length.max(0.0)
    };

    // --- Natural frequency (steel approximation) ---
    // fn = d * sqrt(G) / (2*pi*Na*D^2*sqrt(rho))
    // For steel (rho ~7850 kg/m^3), simplified:
    // fn ≈ 3.568e5 * d / (Na * D^2) Hz  [d, D in mm]
    // For non-steel, use the general formula with rho=7850 as default.
    let natural_freq_hz = 3.568e5 * d / (na * big_d.powi(2));

    // --- Buckling check ---
    // Critical when free_length / D > ~4 for squared/ground ends
    let buckling_critical = if spring_type == "compression" {
        free_length / big_d > 4.0
    } else {
        false
    };

    // --- Stress calculations ---
    // Max shear stress at design load: τ = Kw * 8*F*D / (π*d³)
    let max_shear_stress_mpa = if let Some(f) = design_load {
        kw * 8.0 * f * big_d / (PI * d.powi(3))
    } else {
        // Stress at max deflection (solid): F_solid = k * max_deflection
        if max_deflection > 0.0 {
            let f_solid = k * max_deflection;
            kw * 8.0 * f_solid * big_d / (PI * d.powi(3))
        } else {
            0.0
        }
    };

    // Stress at solid height
    let stress_at_solid_mpa = if spring_type == "compression" && max_deflection > 0.0 {
        let f_solid = k * max_deflection;
        kw * 8.0 * f_solid * big_d / (PI * d.powi(3))
    } else {
        0.0
    };

    // --- Stress check against yield ---
    let stress_ok = material_yield_mpa.map(|yield_s| {
        let check_stress = if design_load.is_some() {
            max_shear_stress_mpa
        } else {
            stress_at_solid_mpa
        };
        check_stress < yield_s
    });

    // --- Helix parameters for cad.helix ---
    let helix_height = free_length;
    let helix_radius = big_d / 2.0;
    let helix_turns = total_coils;
    // Pitch: active region pitch (excluding dead coils at ends)
    let helix_pitch = if na > 0.0 && spring_type == "compression" {
        (free_length - 2.0 * d) / na
    } else {
        // Extension / torsion: coils are tightly wound
        d
    };

    // --- Wire cross-section sketch (circle for sweep) ---
    let wire_cross_section = SketchResult {
        elements: vec![SketchElement::Circle {
            cx: 0.0,
            cy: 0.0,
            r: d / 2.0,
        }],
        metadata: {
            let mut m = HashMap::new();
            m.insert("wire_diameter".into(), d);
            m
        },
    };

    Ok(SpringResult {
        wire_cross_section,
        spring_rate: k,
        wahl_factor: kw,
        solid_height,
        max_deflection,
        natural_freq_hz,
        buckling_critical,
        max_shear_stress_mpa,
        stress_at_solid_mpa,
        stress_ok,
        helix_radius,
        helix_pitch,
        helix_height,
        helix_turns,
        spring_type: spring_type.to_string(),
        end_type: end_type.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Standard compression spring: 2mm wire, 16mm mean coil diameter,
    /// 8 active coils, 50mm free length, steel (G=79.3 GPa).
    fn standard_compression() -> SpringResult {
        helical_spring(
            "compression",
            2.0,
            16.0,
            8.0,
            50.0,
            79.3,
            Some(500.0),
            "closed_ground",
            Some(50.0),
        )
        .unwrap()
    }

    #[test]
    fn test_spring_index_and_wahl() {
        let r = standard_compression();
        let c = 16.0 / 2.0; // C = 8
        let kw_expected = (4.0 * c - 1.0) / (4.0 * c - 4.0) + 0.615 / c;
        assert!((r.wahl_factor - kw_expected).abs() < 1e-10);
    }

    #[test]
    fn test_spring_rate() {
        let r = standard_compression();
        // k = G*d^4 / (8*D^3*Na)
        // G = 79.3e3 MPa, d=2, D=16, Na=8
        let k_expected = 79.3e3 * 2.0_f64.powi(4) / (8.0 * 16.0_f64.powi(3) * 8.0);
        assert!(
            (r.spring_rate - k_expected).abs() < 1e-6,
            "spring_rate={}, expected={}",
            r.spring_rate,
            k_expected
        );
    }

    #[test]
    fn test_solid_height_closed_ground() {
        let r = standard_compression();
        // Ls = (Na + 2) * d = 10 * 2 = 20
        assert!((r.solid_height - 20.0).abs() < 1e-10);
    }

    #[test]
    fn test_solid_height_closed() {
        let r = helical_spring(
            "compression", 2.0, 16.0, 8.0, 50.0, 79.3, None, "closed", None,
        )
        .unwrap();
        // Ls = (Na + 3) * d = 11 * 2 = 22
        assert!((r.solid_height - 22.0).abs() < 1e-10);
    }

    #[test]
    fn test_solid_height_open() {
        let r = helical_spring(
            "compression", 2.0, 16.0, 8.0, 50.0, 79.3, None, "open", None,
        )
        .unwrap();
        // Ls = (Na + 1) * d = 9 * 2 = 18
        assert!((r.solid_height - 18.0).abs() < 1e-10);
    }

    #[test]
    fn test_max_deflection() {
        let r = standard_compression();
        // free_length - solid_height = 50 - 20 = 30
        assert!((r.max_deflection - 30.0).abs() < 1e-10);
    }

    #[test]
    fn test_buckling_critical() {
        // free_length / D = 50 / 16 = 3.125 < 4 → not critical
        let r = standard_compression();
        assert!(!r.buckling_critical);

        // Make it tall: free_length = 100 → 100/16 = 6.25 > 4
        let r2 = helical_spring(
            "compression", 2.0, 16.0, 8.0, 100.0, 79.3, None, "closed_ground", None,
        )
        .unwrap();
        assert!(r2.buckling_critical);
    }

    #[test]
    fn test_natural_frequency_positive() {
        let r = standard_compression();
        assert!(r.natural_freq_hz > 0.0);
        // For d=2, Na=8, D=16: fn ≈ 3.568e5 * 2 / (8 * 256) ≈ 348.4 Hz
        let expected = 3.568e5 * 2.0 / (8.0 * 16.0_f64.powi(2));
        assert!(
            (r.natural_freq_hz - expected).abs() < 1.0,
            "natural_freq_hz={}, expected={}",
            r.natural_freq_hz,
            expected
        );
    }

    #[test]
    fn test_stress_at_design_load() {
        let r = standard_compression();
        // τ = Kw * 8*F*D / (π*d³)
        let expected = r.wahl_factor * 8.0 * 50.0 * 16.0 / (PI * 2.0_f64.powi(3));
        assert!(
            (r.max_shear_stress_mpa - expected).abs() < 1e-6,
            "stress={}, expected={}",
            r.max_shear_stress_mpa,
            expected
        );
    }

    #[test]
    fn test_stress_ok_passes() {
        let r = standard_compression();
        // With yield=500 MPa and moderate load, stress should be OK
        assert_eq!(r.stress_ok, Some(true));
    }

    #[test]
    fn test_stress_ok_fails() {
        // Huge load → stress exceeds yield
        let r = helical_spring(
            "compression", 1.0, 10.0, 5.0, 30.0, 79.3, Some(100.0), "closed_ground",
            Some(500.0),
        )
        .unwrap();
        assert_eq!(r.stress_ok, Some(false));
    }

    #[test]
    fn test_wire_cross_section() {
        let r = standard_compression();
        assert_eq!(r.wire_cross_section.elements.len(), 1);
        match &r.wire_cross_section.elements[0] {
            SketchElement::Circle { cx, cy, r: radius } => {
                assert!((cx - 0.0).abs() < 1e-10);
                assert!((cy - 0.0).abs() < 1e-10);
                assert!((radius - 1.0).abs() < 1e-10); // d/2 = 2/2 = 1
            }
            _ => panic!("Expected Circle element"),
        }
    }

    #[test]
    fn test_helix_params() {
        let r = standard_compression();
        assert!((r.helix_radius - 8.0).abs() < 1e-10); // D/2 = 16/2
        assert!((r.helix_height - 50.0).abs() < 1e-10); // free_length
        assert!((r.helix_turns - 10.0).abs() < 1e-10); // Na + 2 = 10
        // pitch = (free_length - 2*d) / Na = (50 - 4) / 8 = 5.75
        assert!((r.helix_pitch - 5.75).abs() < 1e-10);
    }

    #[test]
    fn test_invalid_spring_type() {
        let r = helical_spring(
            "invalid", 2.0, 16.0, 8.0, 50.0, 79.3, None, "closed_ground", None,
        );
        assert!(r.is_err());
    }

    #[test]
    fn test_invalid_end_type() {
        let r = helical_spring(
            "compression", 2.0, 16.0, 8.0, 50.0, 79.3, None, "invalid", None,
        );
        assert!(r.is_err());
    }

    #[test]
    fn test_wire_diameter_too_large() {
        let r = helical_spring(
            "compression", 20.0, 16.0, 8.0, 50.0, 79.3, None, "closed_ground", None,
        );
        assert!(r.is_err());
    }

    #[test]
    fn test_low_spring_index_rejected() {
        // C = D/d = 4/2 = 2 < 3 → rejected
        let r = helical_spring(
            "compression", 2.0, 4.0, 8.0, 50.0, 79.3, None, "closed_ground", None,
        );
        assert!(r.is_err());
    }

    #[test]
    fn test_extension_spring() {
        let r = helical_spring(
            "extension", 1.5, 12.0, 10.0, 40.0, 79.3, None, "closed_ground", None,
        )
        .unwrap();
        assert_eq!(r.spring_type, "extension");
        assert!(r.spring_rate > 0.0);
        // Extension springs have tightly wound coils, pitch = d
        assert!((r.helix_pitch - 1.5).abs() < 1e-10);
    }
}
