use std::collections::HashMap;

/// A single sketch element that maps directly to cad.sketch element dicts.
#[derive(Debug, Clone)]
pub enum SketchElement {
    Line {
        x1: f64,
        y1: f64,
        x2: f64,
        y2: f64,
    },
    Arc {
        cx: f64,
        cy: f64,
        r: f64,
        start_angle: f64,
        end_angle: f64,
    },
    Circle {
        cx: f64,
        cy: f64,
        r: f64,
    },
    Spline {
        points: Vec<[f64; 2]>,
        degree: u32,
        periodic: bool,
        weights: Option<Vec<f64>>,
    },
}

/// Result of a geometry generator: elements + computed metadata.
#[derive(Debug, Clone)]
pub struct SketchResult {
    pub elements: Vec<SketchElement>,
    pub metadata: HashMap<String, f64>,
}

/// Computed gear parameters.
#[derive(Debug, Clone)]
pub struct GearParams {
    pub module: f64,
    pub teeth: u32,
    pub pressure_angle_deg: f64,
    pub clearance_coeff: f64,
    pub profile_shift: f64,
    pub backlash: f64,
    // Computed diameters
    pub pitch_diameter: f64,
    pub base_diameter: f64,
    pub tip_diameter: f64,
    pub root_diameter: f64,
}

impl GearParams {
    pub fn to_metadata(&self) -> HashMap<String, f64> {
        let mut m = HashMap::new();
        m.insert("module".into(), self.module);
        m.insert("teeth".into(), self.teeth as f64);
        m.insert("pressure_angle_deg".into(), self.pressure_angle_deg);
        m.insert("clearance_coeff".into(), self.clearance_coeff);
        m.insert("profile_shift".into(), self.profile_shift);
        m.insert("backlash".into(), self.backlash);
        m.insert("pitch_diameter".into(), self.pitch_diameter);
        m.insert("base_diameter".into(), self.base_diameter);
        m.insert("tip_diameter".into(), self.tip_diameter);
        m.insert("root_diameter".into(), self.root_diameter);
        m
    }
}

/// Layout result for a planetary gear set.
#[derive(Debug, Clone)]
pub struct PlanetaryLayout {
    pub sun: SketchResult,
    pub planet: SketchResult,
    pub ring: SketchResult,
    pub planet_positions: Vec<[f64; 2]>,
    pub sun_params: GearParams,
    pub planet_params: GearParams,
    pub ring_params: GearParams,
}
