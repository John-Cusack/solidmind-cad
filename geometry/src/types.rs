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

/// Computed epicycloidal gear parameters.
#[derive(Debug, Clone)]
pub struct EpicycloidalGearParams {
    pub module: f64,
    pub teeth: u32,
    pub profile_type: String,
    pub pressure_angle_deg: f64,
    pub addendum_coeff: f64,
    pub dedendum_coeff: f64,
    pub pitch_diameter: f64,
    pub tip_diameter: f64,
    pub root_diameter: f64,
    pub mating_teeth: u32,
    pub mating_clearance: f64,
}

impl EpicycloidalGearParams {
    pub fn to_metadata(&self) -> HashMap<String, f64> {
        let mut m = HashMap::new();
        m.insert("module".into(), self.module);
        m.insert("teeth".into(), self.teeth as f64);
        m.insert("pressure_angle_deg".into(), self.pressure_angle_deg);
        m.insert("addendum_coeff".into(), self.addendum_coeff);
        m.insert("dedendum_coeff".into(), self.dedendum_coeff);
        m.insert("pitch_diameter".into(), self.pitch_diameter);
        m.insert("tip_diameter".into(), self.tip_diameter);
        m.insert("root_diameter".into(), self.root_diameter);
        m.insert("mating_teeth".into(), self.mating_teeth as f64);
        m.insert("mating_clearance".into(), self.mating_clearance);
        m
    }
}

/// Result of spiral generation with optional spring analysis.
#[derive(Debug, Clone)]
pub struct SpiralResult {
    /// Main spiral as 2D spline.
    pub spiral: SketchResult,
    /// Terminal curve / overcoil (if requested).
    pub overcoil: Option<SketchResult>,
    /// Developed length (mm).
    pub developed_length_mm: f64,
    /// Number of turns.
    pub num_turns: f64,
    /// Inner radius (mm).
    pub inner_radius: f64,
    /// Outer radius (mm).
    pub outer_radius: f64,
    /// Spring stiffness (N·m/rad) — present when strip dimensions provided.
    pub stiffness_n_m_per_rad: Option<f64>,
    /// Wall stress (MPa) — present when material properties provided.
    pub wall_stress_mpa: Option<f64>,
    /// Stress check: stress < yield — present when material properties provided.
    pub stress_ok: Option<bool>,
}

/// Swiss lever escapement layout (used by escapement module, not exported to Python).
#[derive(Debug, Clone)]
pub struct EscapementLayout {
    pub escape_tooth_slot: SketchResult,
    pub pallet_fork: SketchResult,
    pub roller_table: SketchResult,
    pub entry_stone_angle_deg: f64,
    pub exit_stone_angle_deg: f64,
    pub entry_stone_length: f64,
    pub exit_stone_length: f64,
    pub drop_angle_deg: f64,
    pub draw_angle_entry_deg: f64,
    pub draw_angle_exit_deg: f64,
    pub safety_action_ok: bool,
    pub horn_clearance_ok: bool,
    pub escape_teeth: u32,
    pub escape_pitch_d: f64,
    pub pallet_center_distance: f64,
}

/// Result of helical spring computation.
#[derive(Debug, Clone)]
pub struct SpringResult {
    /// Wire cross-section as a single circle for sweep.
    pub wire_cross_section: SketchResult,
    /// Spring rate k (N/mm).
    pub spring_rate: f64,
    /// Wahl correction factor Kw.
    pub wahl_factor: f64,
    /// Solid height Ls (mm).
    pub solid_height: f64,
    /// Maximum deflection (mm) = free_length - solid_height.
    pub max_deflection: f64,
    /// Natural frequency (Hz).
    pub natural_freq_hz: f64,
    /// Whether free_length/D > 4 (buckling risk).
    pub buckling_critical: bool,
    /// Max shear stress at design load or solid (MPa).
    pub max_shear_stress_mpa: f64,
    /// Stress at solid height (MPa).
    pub stress_at_solid_mpa: f64,
    /// Whether stress < yield (present when yield given).
    pub stress_ok: Option<bool>,
    /// Helix mean radius D/2 (mm).
    pub helix_radius: f64,
    /// Helix pitch (mm) — active coil spacing.
    pub helix_pitch: f64,
    /// Helix total height (mm) — equals free_length.
    pub helix_height: f64,
    /// Helix total turns (including dead coils).
    pub helix_turns: f64,
    /// Spring type ("compression", "extension", "torsion").
    pub spring_type: String,
    /// End type ("closed_ground", "closed", "open", "open_ground").
    pub end_type: String,
}

/// A single cam segment defining motion over an angular range.
#[derive(Debug, Clone)]
pub struct CamSegment {
    /// Start angle in degrees.
    pub start_angle_deg: f64,
    /// End angle in degrees.
    pub end_angle_deg: f64,
    /// Rise in mm (positive = rise, negative = return, 0 = dwell).
    pub rise_mm: f64,
    /// Motion law: "dwell", "simple_harmonic", "cycloidal",
    /// "polynomial345", "polynomial4567", "constant_velocity".
    pub motion_law: String,
}

/// Result of cam profile generation.
#[derive(Debug, Clone)]
pub struct CamResult {
    /// Closed periodic spline profile.
    pub profile: SketchResult,
    /// Maximum pressure angle (degrees).
    pub max_pressure_angle_deg: f64,
    /// Maximum acceleration magnitude (mm/rad²).
    pub max_acceleration: f64,
    /// Displacement vs angle curve: [angle_deg, displacement_mm].
    pub displacement_curve: Vec<[f64; 2]>,
}

/// Bevel gear parameters computed via Tredgold's approximation.
#[derive(Debug, Clone)]
pub struct BevelGearParams {
    pub module: f64,
    pub teeth: u32,
    pub mate_teeth: u32,
    pub pressure_angle_deg: f64,
    pub shaft_angle_deg: f64,
    pub pitch_cone_angle_deg: f64,
    pub face_width: f64,
    pub outer_cone_distance: f64,
    pub mean_cone_distance: f64,
    pub virtual_teeth: f64,
    pub pitch_diameter: f64,
    pub tip_diameter: f64,
    pub root_diameter: f64,
}

/// Result of worm gear pair generation.
#[derive(Debug, Clone)]
pub struct WormGearResult {
    pub worm_thread: SketchResult,
    pub wheel_profile: SketchResult,
    pub axial_module: f64,
    pub worm_starts: u32,
    pub wheel_teeth: u32,
    pub center_distance: f64,
    pub lead_angle_deg: f64,
    pub efficiency: f64,
    pub self_locking: bool,
    pub worm_pitch_diameter: f64,
    pub wheel_pitch_diameter: f64,
}

/// Result of thread profile generation.
#[derive(Debug, Clone)]
pub struct ThreadResult {
    pub profile: SketchResult,
    pub designation: String,
    pub thread_type: String,
    pub pitch_mm: f64,
    pub major_diameter_mm: f64,
    pub minor_diameter_mm: f64,
    pub pitch_diameter_mm: f64,
    pub thread_angle_deg: f64,
    pub external: bool,
}

/// Layout result for a planetary gear set.
#[derive(Debug, Clone)]
pub struct PlanetaryLayout {
    pub sun: SketchResult,
    pub planet: SketchResult,
    pub ring: SketchResult,
    pub ring_blank: SketchResult,
    pub ring_tooth_slot: SketchResult,
    pub planet_positions: Vec<[f64; 2]>,
    pub sun_params: GearParams,
    pub planet_params: GearParams,
    pub ring_params: GearParams,
}
