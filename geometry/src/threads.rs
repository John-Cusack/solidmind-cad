/// Thread profile generation and ISO/UNC/ACME dimension tables.
///
/// Compiled-in dimension tables for common thread standards, plus 2D
/// cross-section generators for one thread period suitable for helix sweep
/// in FreeCAD.

use std::collections::HashMap;

use crate::types::{SketchElement, SketchResult, ThreadResult};

// ---------------------------------------------------------------------------
// Dimension tables
// ---------------------------------------------------------------------------

/// ISO metric coarse thread entry: (designation, pitch_mm, major_d_mm, pitch_d_mm, minor_d_mm)
const ISO_METRIC_COARSE: &[(&str, f64, f64, f64, f64)] = &[
    ("M1",   0.25,  1.0,   0.838,  0.729),
    ("M1.2", 0.25,  1.2,   1.038,  0.929),
    ("M1.4", 0.3,   1.4,   1.205,  1.075),
    ("M1.6", 0.35,  1.6,   1.373,  1.221),
    ("M1.8", 0.35,  1.8,   1.573,  1.421),
    ("M2",   0.4,   2.0,   1.740,  1.567),
    ("M2.5", 0.45,  2.5,   2.208,  2.013),
    ("M3",   0.5,   3.0,   2.675,  2.459),
    ("M3.5", 0.6,   3.5,   3.110,  2.850),
    ("M4",   0.7,   4.0,   3.545,  3.242),
    ("M5",   0.8,   5.0,   4.480,  4.134),
    ("M6",   1.0,   6.0,   5.350,  4.917),
    ("M7",   1.0,   7.0,   6.350,  5.917),
    ("M8",   1.25,  8.0,   7.188,  6.647),
    ("M10",  1.5,  10.0,   9.026,  8.376),
    ("M12",  1.75, 12.0,  10.863, 10.106),
    ("M14",  2.0,  14.0,  12.701, 11.835),
    ("M16",  2.0,  16.0,  14.701, 13.835),
    ("M18",  2.5,  18.0,  16.376, 15.294),
    ("M20",  2.5,  20.0,  18.376, 17.294),
    ("M22",  2.5,  22.0,  20.376, 19.294),
    ("M24",  3.0,  24.0,  22.051, 20.752),
    ("M27",  3.0,  27.0,  25.051, 23.752),
    ("M30",  3.5,  30.0,  27.727, 26.211),
    ("M36",  4.0,  36.0,  33.402, 31.670),
    ("M42",  4.5,  42.0,  39.077, 37.129),
    ("M48",  5.0,  48.0,  44.752, 42.587),
    ("M56",  5.5,  56.0,  52.428, 50.046),
    ("M64",  6.0,  64.0,  60.103, 57.505),
];

/// ISO metric fine thread entry: (designation, pitch_mm, major_d_mm, pitch_d_mm, minor_d_mm)
const ISO_METRIC_FINE: &[(&str, f64, f64, f64, f64)] = &[
    ("M8x1",     1.0,   8.0,   7.350,  6.917),
    ("M10x1",    1.0,  10.0,   9.350,  8.917),
    ("M10x1.25", 1.25, 10.0,   9.188,  8.647),
    ("M12x1.25", 1.25, 12.0,  11.188, 10.647),
    ("M12x1.5",  1.5,  12.0,  11.026, 10.376),
    ("M16x1.5",  1.5,  16.0,  15.026, 14.376),
    ("M20x1.5",  1.5,  20.0,  19.026, 18.376),
    ("M24x2",    2.0,  24.0,  22.701, 21.835),
];

/// UNC thread entry: (designation, tpi, major_d_mm, pitch_d_mm, minor_d_mm)
/// Pitch in mm = 25.4 / TPI
const UNC_THREADS: &[(&str, f64, f64, f64, f64)] = &[
    ("1/4-20",   1.270,  6.350,  5.537,  5.003),
    ("5/16-18",  1.411,  7.938,  7.034,  6.437),
    ("3/8-16",   1.588,  9.525,  8.509,  7.798),
    ("7/16-14",  1.814, 11.113,  9.963,  9.144),
    ("1/2-13",   1.954, 12.700, 11.430, 10.541),
    ("9/16-12",  2.117, 14.288, 12.913, 11.938),
    ("5/8-11",   2.309, 15.875, 14.376, 13.310),
    ("3/4-10",   2.540, 19.050, 17.399, 16.233),
    ("7/8-9",    2.822, 22.225, 20.391, 19.076),
    ("1-8",      3.175, 25.400, 23.338, 21.851),
    ("1-1/8-7",  3.629, 28.575, 26.252, 24.568),
    ("1-1/4-7",  3.629, 31.750, 29.427, 27.743),
    ("1-3/8-6",  4.233, 34.925, 32.269, 30.307),
    ("1-1/2-6",  4.233, 38.100, 35.444, 33.482),
];

/// ACME thread entry: (designation, pitch_mm, major_d_mm)
/// ACME uses 29° included angle. Pitch_d and minor_d derived from geometry.
const ACME_THREADS: &[(&str, f64, f64)] = &[
    ("1/4-16 ACME",  1.588,  6.350),
    ("5/16-14 ACME", 1.814,  7.938),
    ("3/8-12 ACME",  2.117,  9.525),
    ("1/2-10 ACME",  2.540, 12.700),
    ("5/8-8 ACME",   3.175, 15.875),
    ("3/4-6 ACME",   4.233, 19.050),
    ("7/8-6 ACME",   4.233, 22.225),
    ("1-5 ACME",     5.080, 25.400),
    ("1-1/4-5 ACME", 5.080, 31.750),
    ("1-1/2-4 ACME", 6.350, 38.100),
    ("2-4 ACME",     6.350, 50.800),
];

// ---------------------------------------------------------------------------
// Thread lookup
// ---------------------------------------------------------------------------

/// Thread type enumeration for internal use.
enum ThreadType {
    IsoMetric,
    Unc,
    Acme,
    Buttress,
}

/// Parsed thread dimensions.
struct ThreadDims {
    designation: String,
    thread_type: ThreadType,
    pitch_mm: f64,
    major_d: f64,
    pitch_d: f64,
    minor_d: f64,
    thread_angle_deg: f64,
}

/// Look up thread dimensions from a designation string.
///
/// Supported formats:
/// - "M8" or "M8x1.25" (ISO metric coarse/fine)
/// - "1/2-13" (UNC)
/// - "1/2-10 ACME" (ACME trapezoidal)
/// - "M10 BUTTRESS" (buttress, uses ISO metric dimensions)
fn lookup_thread(designation: &str) -> Result<ThreadDims, String> {
    let desg = designation.trim();
    let desg_upper = desg.to_uppercase();

    // Check for ACME
    if desg_upper.contains("ACME") {
        let lookup = desg_upper.trim();
        for &(name, pitch, major) in ACME_THREADS {
            if name.to_uppercase() == lookup {
                // ACME: pitch_d = major - pitch/2, minor_d = major - pitch
                let pitch_d = major - pitch / 2.0;
                let minor_d = major - pitch;
                return Ok(ThreadDims {
                    designation: name.to_string(),
                    thread_type: ThreadType::Acme,
                    pitch_mm: pitch,
                    major_d: major,
                    pitch_d,
                    minor_d,
                    thread_angle_deg: 29.0,
                });
            }
        }
        return Err(format!("Unknown ACME thread: '{}'. Available: {:?}",
            desg, ACME_THREADS.iter().map(|t| t.0).collect::<Vec<_>>()));
    }

    // Check for buttress
    if desg_upper.contains("BUTTRESS") {
        // Use ISO metric dimensions with buttress angle
        let metric_part = desg_upper.replace("BUTTRESS", "").trim().to_string();
        let dims = lookup_iso_metric(&metric_part)?;
        return Ok(ThreadDims {
            designation: format!("{} BUTTRESS", dims.designation),
            thread_type: ThreadType::Buttress,
            pitch_mm: dims.pitch_mm,
            major_d: dims.major_d,
            pitch_d: dims.pitch_d,
            minor_d: dims.minor_d,
            thread_angle_deg: 45.0, // load flank 7°, trailing flank 45° (we store the larger)
        });
    }

    // Check for ISO metric (starts with M)
    if desg_upper.starts_with('M') {
        let dims = lookup_iso_metric(&desg_upper)?;
        return Ok(dims);
    }

    // Check for UNC (contains '/')
    if desg.contains('/') || desg.contains('-') {
        let lookup = desg.to_uppercase();
        for &(name, pitch, major, pitch_d, minor) in UNC_THREADS {
            if name.to_uppercase() == lookup {
                return Ok(ThreadDims {
                    designation: name.to_string(),
                    thread_type: ThreadType::Unc,
                    pitch_mm: pitch,
                    major_d: major,
                    pitch_d,
                    minor_d: minor,
                    thread_angle_deg: 60.0,
                });
            }
        }
        return Err(format!("Unknown UNC thread: '{}'. Available: {:?}",
            desg, UNC_THREADS.iter().map(|t| t.0).collect::<Vec<_>>()));
    }

    Err(format!("Cannot parse thread designation: '{}'. Use M8, M8x1, 1/2-13, or 1/2-10 ACME format.", desg))
}

/// Look up ISO metric thread (coarse or fine).
fn lookup_iso_metric(desg_upper: &str) -> Result<ThreadDims, String> {
    let desg = desg_upper.trim();

    // Try fine thread first (has 'x' separator, e.g. "M8X1")
    if desg.contains('X') || desg.contains('x') {
        for &(name, pitch, major, pitch_d, minor) in ISO_METRIC_FINE {
            let name_upper = name.to_uppercase().replace('x', "X").replace('X', "x");
            let desg_normalized = desg.to_uppercase().replace('x', "X").replace('X', "x");
            if name_upper == desg_normalized {
                return Ok(ThreadDims {
                    designation: name.to_string(),
                    thread_type: ThreadType::IsoMetric,
                    pitch_mm: pitch,
                    major_d: major,
                    pitch_d,
                    minor_d: minor,
                    thread_angle_deg: 60.0,
                });
            }
        }
        return Err(format!("Unknown ISO metric fine thread: '{}'. Available: {:?}",
            desg, ISO_METRIC_FINE.iter().map(|t| t.0).collect::<Vec<_>>()));
    }

    // Coarse thread (just "M8", "M10", etc.)
    for &(name, pitch, major, pitch_d, minor) in ISO_METRIC_COARSE {
        if name.to_uppercase() == desg {
            return Ok(ThreadDims {
                designation: name.to_string(),
                thread_type: ThreadType::IsoMetric,
                pitch_mm: pitch,
                major_d: major,
                pitch_d,
                minor_d: minor,
                thread_angle_deg: 60.0,
            });
        }
    }

    Err(format!("Unknown ISO metric coarse thread: '{}'. Available: {:?}",
        desg, ISO_METRIC_COARSE.iter().map(|t| t.0).collect::<Vec<_>>()))
}

// ---------------------------------------------------------------------------
// Profile generation
// ---------------------------------------------------------------------------

/// Generate a thread profile cross-section for one period.
///
/// # Arguments
/// * `designation` - Thread designation string (e.g. "M8", "M8x1", "3/8-16", "1/2-10 ACME")
/// * `external` - true for external (bolt) thread, false for internal (nut) thread
/// * `num_points` - Number of points (used for buttress spline; V-thread and ACME use lines)
///
/// # Returns
/// A `ThreadResult` containing the 2D profile (one period) suitable for helix sweep,
/// plus all relevant dimensions.
///
/// The profile is oriented with:
/// - X axis = axial direction (along thread axis)
/// - Y axis = radial direction (from center outward)
/// - Origin at the pitch line center of the thread period
pub fn thread_profile(
    designation: &str,
    external: bool,
    _num_points: usize,
) -> Result<ThreadResult, String> {
    let dims = lookup_thread(designation)?;

    let profile = match dims.thread_type {
        ThreadType::IsoMetric | ThreadType::Unc => {
            generate_v_thread_profile(&dims, external)
        }
        ThreadType::Acme => {
            generate_acme_profile(&dims, external)
        }
        ThreadType::Buttress => {
            generate_buttress_profile(&dims, external)
        }
    };

    let thread_type_str = match dims.thread_type {
        ThreadType::IsoMetric => "ISO_metric",
        ThreadType::Unc => "UNC",
        ThreadType::Acme => "ACME",
        ThreadType::Buttress => "buttress",
    };

    Ok(ThreadResult {
        profile,
        designation: dims.designation,
        thread_type: thread_type_str.to_string(),
        pitch_mm: dims.pitch_mm,
        major_diameter_mm: dims.major_d,
        minor_diameter_mm: dims.minor_d,
        pitch_diameter_mm: dims.pitch_d,
        thread_angle_deg: dims.thread_angle_deg,
        external,
    })
}

/// Generate a 60° V-thread profile (ISO metric / UNC).
///
/// For external threads:
/// - Root flat = pitch/8
/// - Crest flat = pitch/8
/// - Thread height H = pitch * sqrt(3)/2
/// - Actual depth = 5H/8
fn generate_v_thread_profile(dims: &ThreadDims, external: bool) -> SketchResult {
    let p = dims.pitch_mm;

    let major_r = dims.major_d / 2.0;
    let minor_r = dims.minor_d / 2.0;

    // Root and crest flats
    let root_flat = p / 8.0;
    let crest_flat = p / 8.0;

    let (y_crest, y_root) = if external {
        (major_r, minor_r)
    } else {
        // Internal thread: crest is at minor_d, root at major_d
        (minor_r, major_r)
    };

    let depth = (y_crest - y_root).abs();

    // Thread profile centered at x=0 along one pitch
    let half_pitch = p / 2.0;

    let mut elements = Vec::new();

    if external {
        // Root left → left flank → crest → right flank → root right
        let x_root_left = -half_pitch;
        let x_root_right = half_pitch;
        let x_crest_left = -crest_flat / 2.0;
        let x_crest_right = crest_flat / 2.0;

        // Left flank
        elements.push(SketchElement::Line {
            x1: x_root_left + root_flat / 2.0,
            y1: y_root,
            x2: x_crest_left,
            y2: y_crest,
        });

        // Crest
        elements.push(SketchElement::Line {
            x1: x_crest_left,
            y1: y_crest,
            x2: x_crest_right,
            y2: y_crest,
        });

        // Right flank
        elements.push(SketchElement::Line {
            x1: x_crest_right,
            y1: y_crest,
            x2: x_root_right - root_flat / 2.0,
            y2: y_root,
        });

        // Root
        elements.push(SketchElement::Line {
            x1: x_root_right - root_flat / 2.0,
            y1: y_root,
            x2: x_root_left + root_flat / 2.0,
            y2: y_root,
        });
    } else {
        // Internal: root is larger radius, crest is smaller radius
        let x_root_left = -half_pitch;
        let x_root_right = half_pitch;
        let x_crest_left = -crest_flat / 2.0;
        let x_crest_right = crest_flat / 2.0;

        // Left flank (from root down to crest)
        elements.push(SketchElement::Line {
            x1: x_root_left + root_flat / 2.0,
            y1: y_root,
            x2: x_crest_left,
            y2: y_crest,
        });

        // Crest (at minor diameter for internal)
        elements.push(SketchElement::Line {
            x1: x_crest_left,
            y1: y_crest,
            x2: x_crest_right,
            y2: y_crest,
        });

        // Right flank
        elements.push(SketchElement::Line {
            x1: x_crest_right,
            y1: y_crest,
            x2: x_root_right - root_flat / 2.0,
            y2: y_root,
        });

        // Root
        elements.push(SketchElement::Line {
            x1: x_root_right - root_flat / 2.0,
            y1: y_root,
            x2: x_root_left + root_flat / 2.0,
            y2: y_root,
        });
    }

    let mut metadata = HashMap::new();
    metadata.insert("pitch_mm".into(), p);
    metadata.insert("major_diameter_mm".into(), dims.major_d);
    metadata.insert("minor_diameter_mm".into(), dims.minor_d);
    metadata.insert("pitch_diameter_mm".into(), dims.pitch_d);
    metadata.insert("thread_depth_mm".into(), depth);
    metadata.insert("thread_angle_deg".into(), 60.0);
    metadata.insert("root_flat_mm".into(), root_flat);
    metadata.insert("crest_flat_mm".into(), crest_flat);

    SketchResult { elements, metadata }
}

/// Generate a 29° ACME trapezoidal thread profile.
///
/// ACME threads have:
/// - 29° included angle (14.5° each flank from vertical)
/// - Flat root and crest
/// - Thread depth = pitch/2
fn generate_acme_profile(dims: &ThreadDims, external: bool) -> SketchResult {
    let p = dims.pitch_mm;
    let tan_half = 14.5_f64.to_radians().tan();

    let major_r = dims.major_d / 2.0;
    let minor_r = dims.minor_d / 2.0;

    let (y_crest, y_root) = if external {
        (major_r, minor_r)
    } else {
        (minor_r, major_r)
    };

    let depth = (y_crest - y_root).abs();

    // ACME tooth proportions
    let crest_width = (p / 2.0 - depth * tan_half).max(0.4 * p / 2.0);
    let root_width = (p / 2.0 + depth * tan_half).min(0.8 * p);

    let mut elements = Vec::new();

    // Left flank
    elements.push(SketchElement::Line {
        x1: -root_width / 2.0,
        y1: y_root,
        x2: -crest_width / 2.0,
        y2: y_crest,
    });

    // Crest
    elements.push(SketchElement::Line {
        x1: -crest_width / 2.0,
        y1: y_crest,
        x2: crest_width / 2.0,
        y2: y_crest,
    });

    // Right flank
    elements.push(SketchElement::Line {
        x1: crest_width / 2.0,
        y1: y_crest,
        x2: root_width / 2.0,
        y2: y_root,
    });

    // Root
    elements.push(SketchElement::Line {
        x1: root_width / 2.0,
        y1: y_root,
        x2: -root_width / 2.0,
        y2: y_root,
    });

    let mut metadata = HashMap::new();
    metadata.insert("pitch_mm".into(), p);
    metadata.insert("major_diameter_mm".into(), dims.major_d);
    metadata.insert("minor_diameter_mm".into(), dims.minor_d);
    metadata.insert("pitch_diameter_mm".into(), dims.pitch_d);
    metadata.insert("thread_depth_mm".into(), depth);
    metadata.insert("thread_angle_deg".into(), 29.0);
    metadata.insert("crest_width_mm".into(), crest_width);
    metadata.insert("root_width_mm".into(), root_width);

    SketchResult { elements, metadata }
}

/// Generate a buttress thread profile (45°/7°).
///
/// Buttress threads are asymmetric:
/// - Load flank: 7° from vertical (nearly vertical, bears load)
/// - Trailing flank: 45° from vertical (easy engagement)
fn generate_buttress_profile(dims: &ThreadDims, external: bool) -> SketchResult {
    let p = dims.pitch_mm;
    let load_angle = 7.0_f64.to_radians();
    let trail_angle = 45.0_f64.to_radians();

    let major_r = dims.major_d / 2.0;
    let minor_r = dims.minor_d / 2.0;

    let (y_crest, y_root) = if external {
        (major_r, minor_r)
    } else {
        (minor_r, major_r)
    };

    let depth = (y_crest - y_root).abs();

    // Tooth widths based on flank angles
    let load_run = depth * load_angle.tan();   // horizontal run of load flank
    let trail_run = depth * trail_angle.tan();  // horizontal run of trailing flank

    let crest_flat = p / 8.0;

    let mut elements = Vec::new();

    // For external thread, load flank is on the left (bearing side)
    // Trailing flank on the right
    let x_crest_left = -crest_flat / 2.0;
    let x_crest_right = crest_flat / 2.0;

    // Load flank (left): nearly vertical (7°)
    let x_root_left = x_crest_left - load_run;
    // Trailing flank (right): 45°
    let x_root_right = x_crest_right + trail_run;

    // Load flank
    elements.push(SketchElement::Line {
        x1: x_root_left,
        y1: y_root,
        x2: x_crest_left,
        y2: y_crest,
    });

    // Crest
    elements.push(SketchElement::Line {
        x1: x_crest_left,
        y1: y_crest,
        x2: x_crest_right,
        y2: y_crest,
    });

    // Trailing flank
    elements.push(SketchElement::Line {
        x1: x_crest_right,
        y1: y_crest,
        x2: x_root_right,
        y2: y_root,
    });

    // Root
    elements.push(SketchElement::Line {
        x1: x_root_right,
        y1: y_root,
        x2: x_root_left,
        y2: y_root,
    });

    let mut metadata = HashMap::new();
    metadata.insert("pitch_mm".into(), p);
    metadata.insert("major_diameter_mm".into(), dims.major_d);
    metadata.insert("minor_diameter_mm".into(), dims.minor_d);
    metadata.insert("pitch_diameter_mm".into(), dims.pitch_d);
    metadata.insert("thread_depth_mm".into(), depth);
    metadata.insert("load_flank_angle_deg".into(), 7.0);
    metadata.insert("trail_flank_angle_deg".into(), 45.0);

    SketchResult { elements, metadata }
}

#[cfg(test)]
mod tests {
    use super::*;

    // -----------------------------------------------------------------------
    // ISO Metric coarse lookup
    // -----------------------------------------------------------------------

    #[test]
    fn test_m3_dimensions() {
        let result = thread_profile("M3", true, 20).unwrap();
        assert_eq!(result.designation, "M3");
        assert!((result.pitch_mm - 0.5).abs() < 1e-10);
        assert!((result.major_diameter_mm - 3.0).abs() < 1e-10);
        assert!((result.pitch_diameter_mm - 2.675).abs() < 1e-10);
        assert!((result.minor_diameter_mm - 2.459).abs() < 1e-10);
    }

    #[test]
    fn test_m8_coarse_dimensions() {
        let result = thread_profile("M8", true, 20).unwrap();
        assert_eq!(result.designation, "M8");
        assert!((result.pitch_mm - 1.25).abs() < 1e-10);
        assert!((result.major_diameter_mm - 8.0).abs() < 1e-10);
        assert!((result.pitch_diameter_mm - 7.188).abs() < 1e-10);
        assert!((result.minor_diameter_mm - 6.647).abs() < 1e-10);
        assert!((result.thread_angle_deg - 60.0).abs() < 1e-10);
    }

    #[test]
    fn test_m10_dimensions() {
        let result = thread_profile("M10", true, 20).unwrap();
        assert!((result.pitch_mm - 1.5).abs() < 1e-10);
        assert!((result.major_diameter_mm - 10.0).abs() < 1e-10);
        assert!((result.pitch_diameter_mm - 9.026).abs() < 1e-10);
    }

    #[test]
    fn test_m12_dimensions() {
        let result = thread_profile("M12", true, 20).unwrap();
        assert!((result.pitch_mm - 1.75).abs() < 1e-10);
        assert!((result.major_diameter_mm - 12.0).abs() < 1e-10);
        assert!((result.pitch_diameter_mm - 10.863).abs() < 1e-10);
    }

    // -----------------------------------------------------------------------
    // ISO Metric fine lookup
    // -----------------------------------------------------------------------

    #[test]
    fn test_m8x1_fine() {
        let result = thread_profile("M8x1", true, 20).unwrap();
        assert_eq!(result.designation, "M8x1");
        assert!((result.pitch_mm - 1.0).abs() < 1e-10);
        assert!((result.major_diameter_mm - 8.0).abs() < 1e-10);
    }

    #[test]
    fn test_m10x1_25_fine() {
        let result = thread_profile("M10x1.25", true, 20).unwrap();
        assert!((result.pitch_mm - 1.25).abs() < 1e-10);
        assert!((result.major_diameter_mm - 10.0).abs() < 1e-10);
    }

    // -----------------------------------------------------------------------
    // UNC lookup
    // -----------------------------------------------------------------------

    #[test]
    fn test_quarter_20_unc() {
        let result = thread_profile("1/4-20", true, 20).unwrap();
        assert_eq!(result.thread_type, "UNC");
        assert!((result.pitch_mm - 1.270).abs() < 1e-10);
        assert!((result.major_diameter_mm - 6.35).abs() < 1e-10);
    }

    #[test]
    fn test_half_13_unc() {
        let result = thread_profile("1/2-13", true, 20).unwrap();
        assert!((result.major_diameter_mm - 12.7).abs() < 1e-10);
        assert!((result.pitch_mm - 1.954).abs() < 1e-10);
    }

    #[test]
    fn test_three_quarter_10_unc() {
        let result = thread_profile("3/4-10", true, 20).unwrap();
        assert!((result.major_diameter_mm - 19.05).abs() < 1e-10);
    }

    #[test]
    fn test_one_8_unc() {
        let result = thread_profile("1-8", true, 20).unwrap();
        assert!((result.major_diameter_mm - 25.4).abs() < 1e-10);
    }

    // -----------------------------------------------------------------------
    // ACME lookup
    // -----------------------------------------------------------------------

    #[test]
    fn test_half_10_acme() {
        let result = thread_profile("1/2-10 ACME", true, 20).unwrap();
        assert_eq!(result.thread_type, "ACME");
        assert!((result.thread_angle_deg - 29.0).abs() < 1e-10);
        assert!((result.major_diameter_mm - 12.7).abs() < 1e-10);
    }

    // -----------------------------------------------------------------------
    // Profile generation
    // -----------------------------------------------------------------------

    #[test]
    fn test_v_thread_profile_4_lines() {
        let result = thread_profile("M8", true, 20).unwrap();
        let line_count = result.profile.elements.iter().filter(|e| {
            matches!(e, SketchElement::Line { .. })
        }).count();
        assert_eq!(line_count, 4, "V-thread profile should have 4 lines");
    }

    #[test]
    fn test_v_thread_profile_closed() {
        let result = thread_profile("M8", true, 20).unwrap();
        let elems = &result.profile.elements;
        let n = elems.len();
        for i in 0..n {
            let end = line_endpoint(&elems[i], false);
            let start = line_endpoint(&elems[(i + 1) % n], true);
            let gap = ((end[0] - start[0]).powi(2) + (end[1] - start[1]).powi(2)).sqrt();
            assert!(
                gap < 1e-10,
                "V-thread gap of {} between elements {} and {}",
                gap, i, (i + 1) % n,
            );
        }
    }

    #[test]
    fn test_acme_profile_4_lines() {
        let result = thread_profile("1/2-10 ACME", true, 20).unwrap();
        let line_count = result.profile.elements.iter().filter(|e| {
            matches!(e, SketchElement::Line { .. })
        }).count();
        assert_eq!(line_count, 4, "ACME profile should have 4 lines");
    }

    #[test]
    fn test_acme_profile_closed() {
        let result = thread_profile("1/2-10 ACME", true, 20).unwrap();
        let elems = &result.profile.elements;
        let n = elems.len();
        for i in 0..n {
            let end = line_endpoint(&elems[i], false);
            let start = line_endpoint(&elems[(i + 1) % n], true);
            let gap = ((end[0] - start[0]).powi(2) + (end[1] - start[1]).powi(2)).sqrt();
            assert!(
                gap < 1e-10,
                "ACME thread gap of {} between elements {} and {}",
                gap, i, (i + 1) % n,
            );
        }
    }

    #[test]
    fn test_buttress_profile() {
        let result = thread_profile("M10 BUTTRESS", true, 20).unwrap();
        assert_eq!(result.thread_type, "buttress");
        assert!((result.thread_angle_deg - 45.0).abs() < 1e-10);
        assert_eq!(result.profile.elements.len(), 4);
    }

    #[test]
    fn test_internal_thread() {
        let ext = thread_profile("M8", true, 20).unwrap();
        let int = thread_profile("M8", false, 20).unwrap();
        // Same dimensions
        assert!((ext.pitch_mm - int.pitch_mm).abs() < 1e-10);
        assert!((ext.major_diameter_mm - int.major_diameter_mm).abs() < 1e-10);
        // Internal flag differs
        assert!(ext.external);
        assert!(!int.external);
    }

    #[test]
    fn test_unknown_thread_error() {
        let result = thread_profile("M999", true, 20);
        assert!(result.is_err());
    }

    #[test]
    fn test_case_insensitive() {
        let result = thread_profile("m8", true, 20).unwrap();
        assert_eq!(result.designation, "M8");
    }

    #[test]
    fn test_m8x1_25_matches_coarse() {
        // M8 coarse is 1.25, so M8x1.25 should match fine table if present,
        // otherwise it's an error (it's not in the fine table since it IS the coarse pitch).
        // Our fine table doesn't include M8x1.25, only M8x1.
        // So this should fail.
        let result = thread_profile("M8x1.25", true, 20);
        assert!(result.is_err(), "M8x1.25 is not in fine table");
    }

    #[test]
    fn test_profile_metadata() {
        let result = thread_profile("M8", true, 20).unwrap();
        assert!(result.profile.metadata.contains_key("pitch_mm"));
        assert!(result.profile.metadata.contains_key("major_diameter_mm"));
        assert!(result.profile.metadata.contains_key("thread_depth_mm"));
    }

    /// Helper for extracting line endpoints.
    fn line_endpoint(elem: &SketchElement, start: bool) -> [f64; 2] {
        match elem {
            SketchElement::Line { x1, y1, x2, y2 } => {
                if start { [*x1, *y1] } else { [*x2, *y2] }
            }
            _ => panic!("expected Line element"),
        }
    }
}
