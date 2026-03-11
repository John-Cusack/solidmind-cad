use crate::types::{SketchElement, SketchResult};
use std::collections::HashMap;

// ---------------------------------------------------------------------------
// NACA 4-digit airfoil
// ---------------------------------------------------------------------------

/// Parsed NACA 4-digit parameters.
#[derive(Debug, Clone)]
pub struct Naca4 {
    pub max_camber: f64,      // m: fraction of chord
    pub camber_position: f64, // p: fraction of chord
    pub max_thickness: f64,   // t: fraction of chord
}

impl Naca4 {
    /// Parse a NACA 4-digit code like "4412" or "0012".
    pub fn parse(code: &str) -> Result<Naca4, String> {
        let digits = code
            .strip_prefix("NACA")
            .or_else(|| code.strip_prefix("naca"))
            .unwrap_or(code);

        if digits.len() != 4 || !digits.chars().all(|c| c.is_ascii_digit()) {
            return Err(format!(
                "Invalid NACA 4-digit code '{}': need exactly 4 digits (e.g. '4412' or 'NACA4412')",
                code
            ));
        }

        let m = (digits[0..1].parse::<f64>().unwrap()) / 100.0;
        let p = (digits[1..2].parse::<f64>().unwrap()) / 10.0;
        let t = (digits[2..4].parse::<f64>().unwrap()) / 100.0;

        Ok(Naca4 {
            max_camber: m,
            camber_position: p,
            max_thickness: t,
        })
    }

    /// Half-thickness at chord fraction x (0..1).
    fn thickness_at(&self, x: f64) -> f64 {
        let t = self.max_thickness;
        // Standard NACA thickness distribution (open trailing edge variant)
        (t / 0.2)
            * (0.2969 * x.sqrt() - 0.1260 * x - 0.3516 * x * x
                + 0.2843 * x * x * x
                - 0.1015 * x * x * x * x)
    }

    /// Camber and camber gradient at chord fraction x.
    fn camber_at(&self, x: f64) -> (f64, f64) {
        let m = self.max_camber;
        let p = self.camber_position;

        if m < 1e-10 || p < 1e-10 {
            return (0.0, 0.0);
        }

        if x < p {
            let yc = (m / (p * p)) * (2.0 * p * x - x * x);
            let dyc = (2.0 * m / (p * p)) * (p - x);
            (yc, dyc)
        } else {
            let one_p = 1.0 - p;
            let yc = (m / (one_p * one_p)) * ((1.0 - 2.0 * p) + 2.0 * p * x - x * x);
            let dyc = (2.0 * m / (one_p * one_p)) * (p - x);
            (yc, dyc)
        }
    }

    /// Generate upper and lower surface points.
    ///
    /// Returns `(upper, lower)` where each is a `Vec<[f64; 2]>` of (x, y)
    /// points in chord-normalized coordinates (0..1).
    ///
    /// `num_points` is the number of points per surface. Uses cosine spacing
    /// for better resolution at leading and trailing edges.
    pub fn surface_points(&self, num_points: usize) -> (Vec<[f64; 2]>, Vec<[f64; 2]>) {
        let mut upper = Vec::with_capacity(num_points);
        let mut lower = Vec::with_capacity(num_points);

        for i in 0..num_points {
            // Cosine spacing: denser at LE and TE
            let beta = std::f64::consts::PI * (i as f64) / ((num_points - 1) as f64);
            let x = 0.5 * (1.0 - beta.cos());

            let yt = self.thickness_at(x);
            let (yc, dyc) = self.camber_at(x);
            let theta = dyc.atan();

            upper.push([x - yt * theta.sin(), yc + yt * theta.cos()]);
            lower.push([x + yt * theta.sin(), yc - yt * theta.cos()]);
        }

        (upper, lower)
    }

    /// Generate Selig-format .dat string for XFOIL.
    ///
    /// Format: upper surface from TE→LE, then lower surface from LE→TE.
    pub fn to_selig_dat(&self, name: &str, num_points: usize) -> String {
        let (upper, lower) = self.surface_points(num_points);

        let mut lines = Vec::with_capacity(2 * num_points + 1);
        lines.push(name.to_string());

        // Upper surface: trailing edge to leading edge (reverse order)
        for pt in upper.iter().rev() {
            lines.push(format!("  {:.6}  {:.6}", pt[0], pt[1]));
        }
        // Lower surface: leading edge to trailing edge (skip first = LE, already in upper)
        for pt in lower.iter().skip(1) {
            lines.push(format!("  {:.6}  {:.6}", pt[0], pt[1]));
        }

        lines.join("\n")
    }
}

// ---------------------------------------------------------------------------
// Blade section generation
// ---------------------------------------------------------------------------

/// Generate an airfoil sketch section at a given chord and twist.
///
/// The section is in the XZ plane:
/// - X axis = chordwise (scaled by `chord_mm`)
/// - Z axis = thickness direction
/// - Twist rotates about the quarter-chord point
///
/// Returns a `SketchResult` with two splines (upper + lower surfaces).
pub fn airfoil_section(
    naca: &Naca4,
    chord_mm: f64,
    twist_deg: f64,
    num_points: usize,
) -> SketchResult {
    let (upper, lower) = naca.surface_points(num_points);
    let twist_rad = twist_deg.to_radians();
    let cos_t = twist_rad.cos();
    let sin_t = twist_rad.sin();

    // Quarter-chord pivot point (normalized)
    let pivot_x = 0.25;
    let pivot_y = 0.0;

    let transform = |pts: &[[f64; 2]]| -> Vec<[f64; 2]> {
        pts.iter()
            .map(|p| {
                // Scale to chord
                let x = p[0] * chord_mm;
                let y = p[1] * chord_mm;
                // Translate so pivot is at origin
                let dx = x - pivot_x * chord_mm;
                let dy = y - pivot_y * chord_mm;
                // Rotate by twist
                let rx = dx * cos_t - dy * sin_t;
                let ry = dx * sin_t + dy * cos_t;
                // Translate pivot back
                [rx + pivot_x * chord_mm, ry + pivot_y * chord_mm]
            })
            .collect()
    };

    let upper_pts = transform(&upper);
    let lower_pts = transform(&lower);

    let elements = vec![
        SketchElement::Spline {
            points: upper_pts,
            degree: 3,
            periodic: false,
            weights: None,
        },
        SketchElement::Spline {
            points: lower_pts,
            degree: 3,
            periodic: false,
            weights: None,
        },
    ];

    let mut metadata = HashMap::new();
    metadata.insert("chord_mm".into(), chord_mm);
    metadata.insert("twist_deg".into(), twist_deg);

    SketchResult { elements, metadata }
}

// ---------------------------------------------------------------------------
// Full propeller blade
// ---------------------------------------------------------------------------

/// Result of propeller blade generation.
#[derive(Debug, Clone)]
pub struct PropellerResult {
    /// Airfoil section sketch results at each radial station.
    pub sections: Vec<PropellerSection>,
    /// Hub sketch result (circle).
    pub hub: SketchResult,
    pub hub_diameter_mm: f64,
    pub hub_height_mm: f64,
    /// Blade table for BEMT consumption.
    pub blade_table: BladeTable,
    /// Selig-format airfoil dat.
    pub airfoil_dat: String,
    /// Input parameters echoed back.
    pub params: PropellerParams,
}

#[derive(Debug, Clone)]
pub struct PropellerSection {
    pub sketch: SketchResult,
    pub station_radius_mm: f64,
    pub chord_mm: f64,
    pub twist_deg: f64,
    pub plane_offset_mm: f64,
}

#[derive(Debug, Clone)]
pub struct BladeTable {
    pub r_frac: Vec<f64>,
    pub chord_mm: Vec<f64>,
    pub twist_deg: Vec<f64>,
    pub re_at_5000rpm: Vec<f64>,
}

#[derive(Debug, Clone)]
pub struct PropellerParams {
    pub diameter_mm: f64,
    pub pitch_mm: f64,
    pub hub_diameter_mm: f64,
    pub num_blades: u32,
    pub airfoil: String,
    pub chord_root_mm: f64,
    pub chord_tip_mm: f64,
    pub num_sections: usize,
    pub num_points: usize,
}

/// Generate a complete propeller blade definition.
pub fn propeller_blade(
    diameter_mm: f64,
    pitch_mm: f64,
    hub_diameter_mm: f64,
    num_blades: u32,
    airfoil: &str,
    chord_root_mm: Option<f64>,
    chord_tip_mm: Option<f64>,
    num_sections: usize,
    num_points: usize,
) -> Result<PropellerResult, String> {
    // Validate inputs
    if diameter_mm <= 0.0 {
        return Err("diameter must be > 0".into());
    }
    if pitch_mm <= 0.0 {
        return Err("pitch must be > 0".into());
    }
    if hub_diameter_mm >= diameter_mm {
        return Err("hub_diameter must be < diameter".into());
    }
    if hub_diameter_mm <= 0.0 {
        return Err("hub_diameter must be > 0".into());
    }
    if num_blades < 1 {
        return Err("num_blades must be >= 1".into());
    }
    if num_sections < 2 {
        return Err("num_sections must be >= 2".into());
    }

    let naca = Naca4::parse(airfoil)?;

    let radius_mm = diameter_mm / 2.0;
    let hub_radius_mm = hub_diameter_mm / 2.0;

    // Auto-size chords if not specified (~10% of diameter is a typical root chord)
    let chord_root = chord_root_mm.unwrap_or(diameter_mm * 0.12);
    let chord_tip = chord_tip_mm.unwrap_or(chord_root * 0.4);

    let hub_height = hub_diameter_mm * 0.6;

    // Generate sections
    let mut sections = Vec::with_capacity(num_sections);
    let mut blade_table = BladeTable {
        r_frac: Vec::with_capacity(num_sections),
        chord_mm: Vec::with_capacity(num_sections),
        twist_deg: Vec::with_capacity(num_sections),
        re_at_5000rpm: Vec::with_capacity(num_sections),
    };

    for i in 0..num_sections {
        // Evenly spaced from hub to near-tip
        let t = (i as f64 + 0.5) / num_sections as f64;
        let r_mm = hub_radius_mm + t * (radius_mm - hub_radius_mm);
        let r_frac = r_mm / radius_mm;

        // Linear chord taper
        let chord = chord_root + t * (chord_tip - chord_root);

        // Twist from pitch: twist(r) = atan(pitch / (2*pi*r))
        let twist = (pitch_mm / (2.0 * std::f64::consts::PI * r_mm))
            .atan()
            .to_degrees();

        // Generate airfoil section
        let sketch = airfoil_section(&naca, chord, twist, num_points);

        // Estimate Reynolds number at 5000 RPM
        // Re = V * c / nu, V = omega * r, omega = 5000 * 2pi/60
        let omega = 5000.0 * 2.0 * std::f64::consts::PI / 60.0;
        let v_local = omega * (r_mm / 1000.0); // m/s
        let chord_m = chord / 1000.0;
        let nu = 1.5e-5; // kinematic viscosity of air
        let re = (v_local * chord_m / nu) as f64;

        sections.push(PropellerSection {
            sketch,
            station_radius_mm: r_mm,
            chord_mm: chord,
            twist_deg: twist,
            plane_offset_mm: r_mm,
        });

        blade_table.r_frac.push(r_frac);
        blade_table.chord_mm.push(chord);
        blade_table.twist_deg.push(twist);
        blade_table.re_at_5000rpm.push(re.round());
    }

    // Hub sketch: simple circle
    let hub_sketch = SketchResult {
        elements: vec![SketchElement::Circle {
            cx: 0.0,
            cy: 0.0,
            r: hub_radius_mm,
        }],
        metadata: {
            let mut m = HashMap::new();
            m.insert("diameter_mm".into(), hub_diameter_mm);
            m.insert("height_mm".into(), hub_height);
            m
        },
    };

    // Airfoil dat
    let airfoil_dat = naca.to_selig_dat(airfoil, num_points);

    let params = PropellerParams {
        diameter_mm,
        pitch_mm,
        hub_diameter_mm,
        num_blades,
        airfoil: airfoil.to_string(),
        chord_root_mm: chord_root,
        chord_tip_mm: chord_tip,
        num_sections,
        num_points,
    };

    Ok(PropellerResult {
        sections,
        hub: hub_sketch,
        hub_diameter_mm,
        hub_height_mm: hub_height,
        blade_table,
        airfoil_dat,
        params,
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_naca_parse_valid() {
        let n = Naca4::parse("NACA4412").unwrap();
        assert!((n.max_camber - 0.04).abs() < 1e-10);
        assert!((n.camber_position - 0.4).abs() < 1e-10);
        assert!((n.max_thickness - 0.12).abs() < 1e-10);
    }

    #[test]
    fn test_naca_parse_bare_digits() {
        let n = Naca4::parse("0012").unwrap();
        assert!((n.max_camber).abs() < 1e-10);
        assert!((n.max_thickness - 0.12).abs() < 1e-10);
    }

    #[test]
    fn test_naca_parse_invalid() {
        assert!(Naca4::parse("NACA123").is_err());
        assert!(Naca4::parse("abcd").is_err());
        assert!(Naca4::parse("NACA12345").is_err());
    }

    #[test]
    fn test_naca0012_symmetry() {
        let naca = Naca4::parse("NACA0012").unwrap();
        let (upper, lower) = naca.surface_points(30);

        // Symmetric airfoil: upper y = -lower y at each station
        for (u, l) in upper.iter().zip(lower.iter()) {
            assert!(
                (u[0] - l[0]).abs() < 1e-10,
                "x coords should match: {} vs {}",
                u[0],
                l[0]
            );
            assert!(
                (u[1] + l[1]).abs() < 1e-10,
                "upper y should equal -lower y: {} vs {}",
                u[1],
                l[1]
            );
        }
    }

    #[test]
    fn test_naca4412_max_thickness_near_30pct() {
        let naca = Naca4::parse("NACA4412").unwrap();
        let (upper, lower) = naca.surface_points(100);

        // Find station of max thickness
        let mut max_t = 0.0f64;
        let mut max_x = 0.0f64;
        for (u, l) in upper.iter().zip(lower.iter()) {
            let t = u[1] - l[1];
            if t > max_t {
                max_t = t;
                max_x = (u[0] + l[0]) / 2.0;
            }
        }

        // Max thickness should be near x/c = 0.3 (standard NACA 4-digit)
        assert!(
            (max_x - 0.3).abs() < 0.05,
            "max thickness at x/c = {}, expected ~0.3",
            max_x
        );
    }

    #[test]
    fn test_airfoil_closure() {
        let naca = Naca4::parse("NACA2412").unwrap();
        let (upper, lower) = naca.surface_points(40);

        // TE: upper last ≈ lower last (both near x=1)
        let u_te = upper.last().unwrap();
        let l_te = lower.last().unwrap();
        assert!(
            (u_te[0] - l_te[0]).abs() < 0.01,
            "TE x should be close"
        );

        // LE: upper first ≈ lower first (both near x=0)
        let u_le = &upper[0];
        let l_le = &lower[0];
        assert!(
            (u_le[0] - l_le[0]).abs() < 1e-10,
            "LE x should match"
        );
        assert!(
            (u_le[1] - l_le[1]).abs() < 1e-10,
            "LE y should match"
        );
    }

    #[test]
    fn test_twist_decreases_root_to_tip() {
        let result = propeller_blade(254.0, 114.0, 20.0, 2, "NACA4412", None, None, 6, 30)
            .unwrap();

        let twists = &result.blade_table.twist_deg;
        for i in 1..twists.len() {
            assert!(
                twists[i] < twists[i - 1],
                "twist should decrease: station {} ({}) >= station {} ({})",
                i,
                twists[i],
                i - 1,
                twists[i - 1]
            );
        }
    }

    #[test]
    fn test_chord_taper_linear() {
        let result = propeller_blade(254.0, 114.0, 20.0, 2, "NACA4412", Some(30.0), Some(12.0), 6, 30)
            .unwrap();

        let chords = &result.blade_table.chord_mm;
        // Should be monotonically decreasing (root→tip taper)
        for i in 1..chords.len() {
            assert!(
                chords[i] < chords[i - 1],
                "chord should decrease: station {} ({}) >= station {} ({})",
                i,
                chords[i],
                i - 1,
                chords[i - 1]
            );
        }
    }

    #[test]
    fn test_section_count() {
        let result = propeller_blade(254.0, 114.0, 20.0, 2, "NACA4412", None, None, 8, 30)
            .unwrap();
        assert_eq!(result.sections.len(), 8);
        assert_eq!(result.blade_table.r_frac.len(), 8);
    }

    #[test]
    fn test_selig_dat_format() {
        let naca = Naca4::parse("NACA4412").unwrap();
        let dat = naca.to_selig_dat("NACA4412", 20);

        let lines: Vec<&str> = dat.lines().collect();
        assert_eq!(lines[0], "NACA4412");
        assert!(lines.len() > 10);

        // Check that coordinate lines parse as two floats
        for line in &lines[1..] {
            let parts: Vec<&str> = line.split_whitespace().collect();
            assert_eq!(parts.len(), 2, "each line should have 2 values: {:?}", line);
            parts[0].parse::<f64>().expect("x should be float");
            parts[1].parse::<f64>().expect("y should be float");
        }
    }

    #[test]
    fn test_error_hub_larger_than_diameter() {
        let result = propeller_blade(100.0, 50.0, 120.0, 2, "NACA4412", None, None, 6, 30);
        assert!(result.is_err());
    }

    #[test]
    fn test_error_invalid_naca() {
        let result = propeller_blade(254.0, 114.0, 20.0, 2, "NACA123", None, None, 6, 30);
        assert!(result.is_err());
    }
}
