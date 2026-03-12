mod bevel;
mod cam;
mod crossing;
mod escapement;
mod gears;
mod involute;
mod pinion;
mod planetary;
mod propeller;
mod spirals;
mod spring;
mod threads;
mod types;
mod worm;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use bevel::{compute_bevel_gear_params, bevel_gear_profile};
use cam::cam_profile as cam_profile_impl;
use crossing::spoke_pattern as spoke_pattern_impl;
use gears::{
    compute_gear_params, compute_internal_gear_params, internal_gear_profile, single_tooth_slot,
    spur_gear_profile,
};
use involute::involute_curve_points;
use pinion::{compute_epicycloidal_gear_params, epicycloidal_tooth_slot as epicycloidal_tooth_slot_impl};
use planetary::planetary_layout as planetary_layout_impl;
use spirals::spiral as spiral_impl;
use spring::helical_spring as helical_spring_impl;
use threads::thread_profile as thread_profile_impl;
use types::{CamSegment, EpicycloidalGearParams, GearParams, SketchElement, SketchResult};
use worm::worm_gear_pair as worm_gear_pair_impl;

// ---------------------------------------------------------------------------
// PyO3 conversion helpers
// ---------------------------------------------------------------------------

fn sketch_element_to_py(py: Python<'_>, elem: &SketchElement) -> PyResult<PyObject> {
    let d = PyDict::new_bound(py);
    match elem {
        SketchElement::Line { x1, y1, x2, y2 } => {
            d.set_item("type", "line")?;
            d.set_item("x1", x1)?;
            d.set_item("y1", y1)?;
            d.set_item("x2", x2)?;
            d.set_item("y2", y2)?;
        }
        SketchElement::Arc { cx, cy, r, start_angle, end_angle } => {
            d.set_item("type", "arc")?;
            d.set_item("cx", cx)?;
            d.set_item("cy", cy)?;
            d.set_item("r", r)?;
            d.set_item("start_angle", start_angle)?;
            d.set_item("end_angle", end_angle)?;
        }
        SketchElement::Circle { cx, cy, r } => {
            d.set_item("type", "circle")?;
            d.set_item("cx", cx)?;
            d.set_item("cy", cy)?;
            d.set_item("r", r)?;
        }
        SketchElement::Spline { points, degree, periodic, weights } => {
            d.set_item("type", "spline")?;
            let py_pts: Vec<Vec<f64>> = points.iter().map(|p| vec![p[0], p[1]]).collect();
            d.set_item("points", py_pts)?;
            d.set_item("degree", degree)?;
            d.set_item("periodic", periodic)?;
            if let Some(w) = weights {
                d.set_item("weights", w.clone())?;
            }
        }
    }
    Ok(d.unbind().into())
}

fn sketch_result_to_py(py: Python<'_>, result: &SketchResult) -> PyResult<PyObject> {
    let d = PyDict::new_bound(py);
    let elements = PyList::empty_bound(py);
    for elem in &result.elements {
        elements.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("elements", elements)?;

    let params = PyDict::new_bound(py);
    for (k, v) in &result.metadata {
        params.set_item(k, v)?;
    }
    d.set_item("params", params)?;
    Ok(d.unbind().into())
}

fn gear_params_to_py(py: Python<'_>, params: &GearParams) -> PyResult<PyObject> {
    let d = PyDict::new_bound(py);
    for (k, v) in params.to_metadata() {
        d.set_item(k, v)?;
    }
    Ok(d.unbind().into())
}

fn epicycloidal_params_to_py(py: Python<'_>, params: &EpicycloidalGearParams) -> PyResult<PyObject> {
    let d = PyDict::new_bound(py);
    for (k, v) in params.to_metadata() {
        d.set_item(k, v)?;
    }
    d.set_item("profile_type", &params.profile_type)?;
    Ok(d.unbind().into())
}

// ---------------------------------------------------------------------------
// Existing gear tools
// ---------------------------------------------------------------------------

#[pyfunction]
#[pyo3(signature = (
    module, teeth, pressure_angle_deg = 20.0, clearance_coeff = 0.25,
    profile_shift = 0.0, backlash = 0.0, center_x = 0.0, center_y = 0.0,
    num_involute_pts = 20, internal = false
))]
fn spur_gear(
    py: Python<'_>, module: f64, teeth: u32, pressure_angle_deg: f64,
    clearance_coeff: f64, profile_shift: f64, backlash: f64,
    center_x: f64, center_y: f64, num_involute_pts: usize, internal: bool,
) -> PyResult<PyObject> {
    let center = [center_x, center_y];
    let (params, result) = if internal {
        let p = compute_internal_gear_params(module, teeth, pressure_angle_deg, clearance_coeff, profile_shift, backlash);
        let r = internal_gear_profile(&p, center, num_involute_pts);
        (p, r)
    } else {
        let p = compute_gear_params(module, teeth, pressure_angle_deg, clearance_coeff, profile_shift, backlash);
        let r = spur_gear_profile(&p, center, num_involute_pts);
        (p, r)
    };

    let d = PyDict::new_bound(py);
    let elements = PyList::empty_bound(py);
    for elem in &result.elements {
        elements.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("elements", elements)?;
    d.set_item("params", gear_params_to_py(py, &params)?)?;

    let meta = &result.metadata;
    let params_dict = PyDict::new_bound(py);
    for (k, v) in meta {
        params_dict.set_item(k, v)?;
    }
    d.set_item("metadata", params_dict)?;
    d.set_item("build_hint", if internal {
        "Use these elements in a cad.sketch, then cad.pad for the ring gear body. \
         You may need a separate outer circle for the ring housing."
    } else {
        "Use these elements in a cad.sketch, then cad.pad to extrude the gear. \
         Add a center bore with cad.hole if needed."
    })?;
    Ok(d.unbind().into())
}

#[pyfunction]
#[pyo3(signature = (
    module, teeth, pressure_angle_deg = 20.0, clearance_coeff = 0.25,
    profile_shift = 0.0, backlash = 0.0, center_x = 0.0, center_y = 0.0,
    num_involute_pts = 20
))]
fn tooth_slot(
    py: Python<'_>, module: f64, teeth: u32, pressure_angle_deg: f64,
    clearance_coeff: f64, profile_shift: f64, backlash: f64,
    center_x: f64, center_y: f64, num_involute_pts: usize,
) -> PyResult<PyObject> {
    let params = compute_gear_params(module, teeth, pressure_angle_deg, clearance_coeff, profile_shift, backlash);
    let center = [center_x, center_y];
    let result = single_tooth_slot(&params, center, num_involute_pts);

    let d = PyDict::new_bound(py);
    let elements = PyList::empty_bound(py);
    for elem in &result.elements {
        elements.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("elements", elements)?;
    d.set_item("params", gear_params_to_py(py, &params)?)?;
    d.set_item("teeth", teeth)?;
    d.set_item("build_hint",
        "1. Create a blank cylinder: cad.sketch (circle, r=tip_diameter/2) → cad.pad\n\
         2. cad.sketch with these elements → cad.pocket (ThroughAll)\n\
         3. cad.polar_pattern(features=['Pocket'], occurrences=teeth)")?;
    Ok(d.unbind().into())
}

#[pyfunction]
#[pyo3(signature = (
    module, teeth, pressure_angle_deg = 20.0, clearance_coeff = 0.25,
    profile_shift = 0.0, backlash = 0.0, internal = false
))]
fn gear_params(
    py: Python<'_>, module: f64, teeth: u32, pressure_angle_deg: f64,
    clearance_coeff: f64, profile_shift: f64, backlash: f64, internal: bool,
) -> PyResult<PyObject> {
    let p = if internal {
        compute_internal_gear_params(module, teeth, pressure_angle_deg, clearance_coeff, profile_shift, backlash)
    } else {
        compute_gear_params(module, teeth, pressure_angle_deg, clearance_coeff, profile_shift, backlash)
    };
    gear_params_to_py(py, &p)
}

#[pyfunction]
#[pyo3(signature = (base_radius, start_radius, end_radius, num_points = 20))]
fn involute_points(base_radius: f64, start_radius: f64, end_radius: f64, num_points: usize) -> Vec<(f64, f64)> {
    involute_curve_points(base_radius, start_radius, end_radius, num_points)
        .into_iter()
        .map(|p| (p[0], p[1]))
        .collect()
}

#[pyfunction]
#[pyo3(signature = (
    module, sun_teeth, planet_teeth, num_planets = 3, pressure_angle_deg = 20.0,
    clearance_coeff = 0.25, profile_shift = 0.0, backlash = 0.0,
    center_x = 0.0, center_y = 0.0, num_involute_pts = 20
))]
fn planetary_layout(
    py: Python<'_>, module: f64, sun_teeth: u32, planet_teeth: u32,
    num_planets: u32, pressure_angle_deg: f64, clearance_coeff: f64,
    profile_shift: f64, backlash: f64, center_x: f64, center_y: f64,
    num_involute_pts: usize,
) -> PyResult<PyObject> {
    let center = [center_x, center_y];
    let layout = planetary_layout_impl(
        module, sun_teeth, planet_teeth, num_planets, pressure_angle_deg,
        clearance_coeff, profile_shift, backlash, center, num_involute_pts,
    ).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

    let d = PyDict::new_bound(py);
    d.set_item("sun", sketch_result_to_py(py, &layout.sun)?)?;
    d.set_item("planet", sketch_result_to_py(py, &layout.planet)?)?;
    d.set_item("ring", sketch_result_to_py(py, &layout.ring)?)?;
    d.set_item("ring_blank", sketch_result_to_py(py, &layout.ring_blank)?)?;
    d.set_item("ring_tooth_slot", sketch_result_to_py(py, &layout.ring_tooth_slot)?)?;
    d.set_item("planet_positions", layout.planet_positions.to_vec())?;

    let params = PyDict::new_bound(py);
    params.set_item("sun", gear_params_to_py(py, &layout.sun_params)?)?;
    params.set_item("planet", gear_params_to_py(py, &layout.planet_params)?)?;
    params.set_item("ring", gear_params_to_py(py, &layout.ring_params)?)?;
    params.set_item("ring_teeth", layout.ring_params.teeth)?;
    d.set_item("params", params)?;
    Ok(d.unbind().into())
}

#[pyfunction]
#[pyo3(signature = (
    diameter, pitch, hub_diameter, num_blades = 2, airfoil = "NACA4412",
    chord_root = None, chord_tip = None, num_sections = 6, num_points = 40
))]
fn propeller_blade_py(
    py: Python<'_>, diameter: f64, pitch: f64, hub_diameter: f64,
    num_blades: u32, airfoil: &str, chord_root: Option<f64>, chord_tip: Option<f64>,
    num_sections: usize, num_points: usize,
) -> PyResult<PyObject> {
    let result = propeller::propeller_blade(
        diameter, pitch, hub_diameter, num_blades, airfoil,
        chord_root, chord_tip, num_sections, num_points,
    ).map_err(|e| pyo3::exceptions::PyValueError::new_err(e))?;

    let d = PyDict::new_bound(py);

    let sections_list = PyList::empty_bound(py);
    for sec in &result.sections {
        let sec_dict = PyDict::new_bound(py);
        let elements = PyList::empty_bound(py);
        for elem in &sec.sketch.elements {
            elements.append(sketch_element_to_py(py, elem)?)?;
        }
        sec_dict.set_item("elements", elements)?;
        sec_dict.set_item("station_radius_mm", sec.station_radius_mm)?;
        sec_dict.set_item("chord_mm", sec.chord_mm)?;
        sec_dict.set_item("twist_deg", sec.twist_deg)?;
        sec_dict.set_item("plane_offset_mm", sec.plane_offset_mm)?;
        sections_list.append(sec_dict)?;
    }
    d.set_item("sections", sections_list)?;

    let hub_dict = PyDict::new_bound(py);
    let hub_elements = PyList::empty_bound(py);
    for elem in &result.hub.elements {
        hub_elements.append(sketch_element_to_py(py, elem)?)?;
    }
    hub_dict.set_item("elements", hub_elements)?;
    hub_dict.set_item("diameter_mm", result.hub_diameter_mm)?;
    hub_dict.set_item("height_mm", result.hub_height_mm)?;
    d.set_item("hub", hub_dict)?;

    let bt = PyDict::new_bound(py);
    bt.set_item("r_frac", result.blade_table.r_frac.clone())?;
    bt.set_item("chord_mm", result.blade_table.chord_mm.clone())?;
    bt.set_item("twist_deg", result.blade_table.twist_deg.clone())?;
    bt.set_item("Re_at_5000rpm", result.blade_table.re_at_5000rpm.clone())?;
    d.set_item("blade_table", bt)?;
    d.set_item("airfoil_dat", &result.airfoil_dat)?;

    let params = PyDict::new_bound(py);
    params.set_item("diameter_mm", result.params.diameter_mm)?;
    params.set_item("pitch_mm", result.params.pitch_mm)?;
    params.set_item("hub_diameter_mm", result.params.hub_diameter_mm)?;
    params.set_item("num_blades", result.params.num_blades)?;
    params.set_item("airfoil", &result.params.airfoil)?;
    params.set_item("chord_root_mm", result.params.chord_root_mm)?;
    params.set_item("chord_tip_mm", result.params.chord_tip_mm)?;
    params.set_item("num_sections", result.params.num_sections)?;
    params.set_item("num_points", result.params.num_points)?;
    d.set_item("params", params)?;
    Ok(d.unbind().into())
}

// ---------------------------------------------------------------------------
// New generalized tools
// ---------------------------------------------------------------------------

/// Generate an epicycloidal gear tooth slot for pocket + polar_pattern.
///
/// Supports epicycloidal, ogival, and modified involute profiles — preferred
/// over standard involute for low tooth counts where involute undercuts.
#[pyfunction]
#[pyo3(signature = (
    module, teeth, mating_teeth,
    profile_type = "epicycloidal", pressure_angle_deg = 15.0,
    addendum_coeff = 0.75, dedendum_coeff = 0.85, backlash = 0.0,
    tip_rounding_r = 0.0, root_fillet_r = 0.0,
    center_x = 0.0, center_y = 0.0, num_points = 20
))]
fn epicycloidal_tooth_slot(
    py: Python<'_>, module: f64, teeth: u32, mating_teeth: u32,
    profile_type: &str, pressure_angle_deg: f64,
    addendum_coeff: f64, dedendum_coeff: f64, backlash: f64,
    tip_rounding_r: f64, root_fillet_r: f64,
    center_x: f64, center_y: f64, num_points: usize,
) -> PyResult<PyObject> {
    if teeth < 4 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            format!("teeth must be >= 4, got {}", teeth),
        ));
    }
    let valid_types = ["epicycloidal", "modified_involute", "ogival"];
    if !valid_types.contains(&profile_type) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            format!("profile_type must be one of {:?}, got '{}'", valid_types, profile_type),
        ));
    }

    let params = compute_epicycloidal_gear_params(
        module, teeth, mating_teeth, profile_type,
        pressure_angle_deg, addendum_coeff, dedendum_coeff, backlash,
    );
    let result = epicycloidal_tooth_slot_impl(&params, [center_x, center_y], tip_rounding_r, root_fillet_r, num_points);

    let d = PyDict::new_bound(py);
    let elements = PyList::empty_bound(py);
    for elem in &result.elements {
        elements.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("elements", elements)?;
    d.set_item("params", epicycloidal_params_to_py(py, &params)?)?;
    d.set_item("teeth", teeth)?;
    d.set_item("build_hint",
        "1. Create a blank cylinder: cad.sketch (circle, r=tip_diameter/2) → cad.pad\n\
         2. cad.sketch with these elements → cad.pocket (ThroughAll)\n\
         3. cad.polar_pattern(features=['Pocket'], occurrences=teeth)")?;
    Ok(d.unbind().into())
}

/// Generate an Archimedean spiral with optional spring analysis and terminal curve.
#[pyfunction]
#[pyo3(signature = (
    inner_radius, outer_radius, num_turns,
    num_points_per_turn = 20, center_x = 0.0, center_y = 0.0,
    strip_thickness_mm = None, strip_height_mm = None,
    material_e_gpa = None, material_yield_mpa = None,
    overcoil_angle_deg = None, overcoil_style = None
))]
fn spiral_py(
    py: Python<'_>,
    inner_radius: f64, outer_radius: f64, num_turns: f64,
    num_points_per_turn: usize, center_x: f64, center_y: f64,
    strip_thickness_mm: Option<f64>, strip_height_mm: Option<f64>,
    material_e_gpa: Option<f64>, material_yield_mpa: Option<f64>,
    overcoil_angle_deg: Option<f64>, overcoil_style: Option<&str>,
) -> PyResult<PyObject> {
    if inner_radius >= outer_radius {
        return Err(pyo3::exceptions::PyValueError::new_err("inner_radius must be less than outer_radius"));
    }
    if num_turns < 0.5 {
        return Err(pyo3::exceptions::PyValueError::new_err("num_turns must be >= 0.5"));
    }

    let result = spiral_impl(
        inner_radius, outer_radius, num_turns, num_points_per_turn,
        center_x, center_y, strip_thickness_mm, strip_height_mm,
        material_e_gpa, material_yield_mpa, overcoil_angle_deg, overcoil_style,
    );

    let d = PyDict::new_bound(py);
    d.set_item("spiral", sketch_result_to_py(py, &result.spiral)?)?;
    if let Some(ref oc) = result.overcoil {
        d.set_item("overcoil", sketch_result_to_py(py, oc)?)?;
    }

    let params = PyDict::new_bound(py);
    params.set_item("developed_length_mm", result.developed_length_mm)?;
    params.set_item("num_turns", result.num_turns)?;
    params.set_item("inner_radius", result.inner_radius)?;
    params.set_item("outer_radius", result.outer_radius)?;
    if let Some(k) = result.stiffness_n_m_per_rad {
        params.set_item("stiffness_n_m_per_rad", k)?;
    }
    if let Some(s) = result.wall_stress_mpa {
        params.set_item("wall_stress_mpa", s)?;
    }
    if let Some(ok) = result.stress_ok {
        params.set_item("stress_ok", ok)?;
    }
    d.set_item("params", params)?;

    d.set_item("build_hint",
        "Use spiral elements in cad.sketch → cad.sweep with a cross-section profile. \
         If overcoil is present, build it as a separate sweep.")?;
    Ok(d.unbind().into())
}

/// Generate a spoke pocket profile for pocket + polar_pattern workflow.
#[pyfunction]
#[pyo3(signature = (
    hub_diameter, rim_inner_diameter, rim_outer_diameter,
    num_spokes = 4, spoke_style = "straight",
    spoke_width_hub = 0.8, spoke_width_rim = 0.6,
    fillet_radius_hub = 0.1, fillet_radius_rim = 0.1,
    center_x = 0.0, center_y = 0.0, num_points = 10
))]
fn spoke_pattern_py(
    py: Python<'_>,
    hub_diameter: f64, rim_inner_diameter: f64, rim_outer_diameter: f64,
    num_spokes: u32, spoke_style: &str,
    spoke_width_hub: f64, spoke_width_rim: f64,
    fillet_radius_hub: f64, fillet_radius_rim: f64,
    center_x: f64, center_y: f64, num_points: usize,
) -> PyResult<PyObject> {
    if num_spokes < 2 || num_spokes > 12 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            format!("num_spokes must be 2-12, got {}", num_spokes),
        ));
    }
    let valid_styles = ["straight", "tapered", "curved_s", "curved_c"];
    if !valid_styles.contains(&spoke_style) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            format!("spoke_style must be one of {:?}, got '{}'", valid_styles, spoke_style),
        ));
    }

    let result = spoke_pattern_impl(
        hub_diameter, rim_inner_diameter, rim_outer_diameter, num_spokes,
        spoke_style, spoke_width_hub, spoke_width_rim,
        fillet_radius_hub, fillet_radius_rim, center_x, center_y, num_points,
    );

    let d = PyDict::new_bound(py);
    let elements = PyList::empty_bound(py);
    for elem in &result.elements {
        elements.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("elements", elements)?;

    let params = PyDict::new_bound(py);
    for (k, v) in &result.metadata {
        params.set_item(k, v)?;
    }
    d.set_item("params", params)?;
    d.set_item("num_spokes", num_spokes)?;
    d.set_item("build_hint",
        "1. Build blank: cad.sketch(circle r=rim_outer_diameter/2) → cad.pad\n\
         2. Cut bore: cad.sketch(circle r=bore) → cad.pocket(ThroughAll)\n\
         3. Cut spoke pocket: cad.sketch(these elements) → cad.pocket(ThroughAll)\n\
         4. cad.polar_pattern(features=['Pocket'], occurrences=num_spokes)")?;
    Ok(d.unbind().into())
}

// ---------------------------------------------------------------------------
// Phase 2: Rust gear extensions
// ---------------------------------------------------------------------------

/// Generate a bevel gear tooth slot profile using Tredgold's approximation.
#[pyfunction]
#[pyo3(signature = (
    module, teeth, mate_teeth, pressure_angle_deg = 20.0,
    shaft_angle_deg = 90.0, face_width = 0.0,
    center_x = 0.0, center_y = 0.0, num_involute_pts = 20
))]
fn bevel_gear(
    py: Python<'_>, module: f64, teeth: u32, mate_teeth: u32,
    pressure_angle_deg: f64, shaft_angle_deg: f64, face_width: f64,
    center_x: f64, center_y: f64, num_involute_pts: usize,
) -> PyResult<PyObject> {
    let fw = if face_width > 0.0 { Some(face_width) } else { None };
    let params = compute_bevel_gear_params(
        module, teeth, mate_teeth, pressure_angle_deg, shaft_angle_deg, fw,
    );
    let result = bevel_gear_profile(&params, [center_x, center_y], num_involute_pts);

    let d = PyDict::new_bound(py);
    let elements = PyList::empty_bound(py);
    for elem in &result.elements {
        elements.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("elements", elements)?;

    let p = PyDict::new_bound(py);
    p.set_item("module", params.module)?;
    p.set_item("teeth", params.teeth)?;
    p.set_item("mate_teeth", params.mate_teeth)?;
    p.set_item("pressure_angle_deg", params.pressure_angle_deg)?;
    p.set_item("shaft_angle_deg", params.shaft_angle_deg)?;
    p.set_item("pitch_cone_angle_deg", params.pitch_cone_angle_deg)?;
    p.set_item("face_width", params.face_width)?;
    p.set_item("outer_cone_distance", params.outer_cone_distance)?;
    p.set_item("mean_cone_distance", params.mean_cone_distance)?;
    p.set_item("virtual_teeth", params.virtual_teeth)?;
    p.set_item("pitch_diameter", params.pitch_diameter)?;
    p.set_item("tip_diameter", params.tip_diameter)?;
    p.set_item("root_diameter", params.root_diameter)?;
    d.set_item("params", p)?;
    d.set_item("teeth", teeth)?;
    d.set_item("build_hint",
        "1. Create a blank cone: cad.sketch (circle, r=tip_diameter/2) → cad.pad → cad.draft\n\
         2. cad.sketch with these elements → cad.pocket (ThroughAll)\n\
         3. cad.polar_pattern(features=['Pocket'], occurrences=teeth)")?;
    Ok(d.unbind().into())
}

/// Generate worm gear pair geometry (worm thread + wheel tooth profile).
#[pyfunction]
#[pyo3(signature = (
    axial_module, worm_starts, wheel_teeth, pressure_angle_deg = 20.0,
    worm_pitch_diameter = 0.0, center_x = 0.0, center_y = 0.0, num_points = 20
))]
fn worm_gear(
    py: Python<'_>, axial_module: f64, worm_starts: u32, wheel_teeth: u32,
    pressure_angle_deg: f64, worm_pitch_diameter: f64,
    center_x: f64, center_y: f64, num_points: usize,
) -> PyResult<PyObject> {
    let wpd = if worm_pitch_diameter > 0.0 { Some(worm_pitch_diameter) } else { None };
    let result = worm_gear_pair_impl(
        axial_module, worm_starts, wheel_teeth, pressure_angle_deg,
        wpd, [center_x, center_y], num_points,
    );

    let d = PyDict::new_bound(py);

    // Worm thread elements
    let worm_elems = PyList::empty_bound(py);
    for elem in &result.worm_thread.elements {
        worm_elems.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("worm_thread_elements", worm_elems)?;

    // Wheel profile elements
    let wheel_elems = PyList::empty_bound(py);
    for elem in &result.wheel_profile.elements {
        wheel_elems.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("wheel_profile_elements", wheel_elems)?;

    let p = PyDict::new_bound(py);
    p.set_item("axial_module", result.axial_module)?;
    p.set_item("worm_starts", result.worm_starts)?;
    p.set_item("wheel_teeth", result.wheel_teeth)?;
    p.set_item("center_distance", result.center_distance)?;
    p.set_item("lead_angle_deg", result.lead_angle_deg)?;
    p.set_item("efficiency", result.efficiency)?;
    p.set_item("self_locking", result.self_locking)?;
    p.set_item("worm_pitch_diameter", result.worm_pitch_diameter)?;
    p.set_item("wheel_pitch_diameter", result.wheel_pitch_diameter)?;
    d.set_item("params", p)?;

    d.set_item("build_hint",
        "Worm: cad.helix with lead angle, sweep worm_thread cross-section.\n\
         Wheel: Create blank cylinder (r=wheel_pitch_diameter/2 + module), \
         pocket wheel_profile, polar_pattern for all teeth.")?;
    Ok(d.unbind().into())
}

/// Generate a thread profile cross-section for one period.
#[pyfunction]
#[pyo3(signature = (designation, thread_type = "", external = true, num_points = 20))]
fn thread_profile(
    py: Python<'_>, designation: &str, thread_type: &str,
    external: bool, num_points: usize,
) -> PyResult<PyObject> {
    let result = thread_profile_impl(designation, external, num_points)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e))?;

    let d = PyDict::new_bound(py);
    let elements = PyList::empty_bound(py);
    for elem in &result.profile.elements {
        elements.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("elements", elements)?;

    let p = PyDict::new_bound(py);
    p.set_item("designation", &result.designation)?;
    p.set_item("thread_type", &result.thread_type)?;
    p.set_item("pitch_mm", result.pitch_mm)?;
    p.set_item("major_diameter_mm", result.major_diameter_mm)?;
    p.set_item("minor_diameter_mm", result.minor_diameter_mm)?;
    p.set_item("pitch_diameter_mm", result.pitch_diameter_mm)?;
    p.set_item("thread_angle_deg", result.thread_angle_deg)?;
    p.set_item("external", result.external)?;
    d.set_item("params", p)?;

    d.set_item("build_hint",
        "1. Create a cylinder (r = major_diameter/2 for external, minor/2 for internal)\n\
         2. cad.helix(pitch=pitch_mm, height=thread_length, radius=major_diameter/2)\n\
         3. cad.sketch(geometry_ref=...) on helix start → cad.sweep along helix")?;
    Ok(d.unbind().into())
}

// ---------------------------------------------------------------------------
// Phase 3: Complex mechanisms
// ---------------------------------------------------------------------------

/// Compute helical spring design parameters and wire cross-section.
#[pyfunction]
#[pyo3(signature = (
    spring_type = "compression", wire_diameter = 1.0, coil_diameter = 10.0,
    active_coils = 8.0, free_length = 40.0, material_g_gpa = 79.3,
    material_yield_mpa = -1.0, end_type = "closed_ground", design_load = -1.0
))]
fn helical_spring(
    py: Python<'_>, spring_type: &str, wire_diameter: f64, coil_diameter: f64,
    active_coils: f64, free_length: f64, material_g_gpa: f64,
    material_yield_mpa: f64, end_type: &str, design_load: f64,
) -> PyResult<PyObject> {
    let yield_opt = if material_yield_mpa > 0.0 { Some(material_yield_mpa) } else { None };
    let load_opt = if design_load > 0.0 { Some(design_load) } else { None };

    let result = helical_spring_impl(
        spring_type, wire_diameter, coil_diameter, active_coils, free_length,
        material_g_gpa, yield_opt, end_type, load_opt,
    ).map_err(|e| pyo3::exceptions::PyValueError::new_err(e))?;

    let d = PyDict::new_bound(py);

    // Wire cross-section elements
    let wire_elems = PyList::empty_bound(py);
    for elem in &result.wire_cross_section.elements {
        wire_elems.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("wire_elements", wire_elems)?;

    // Helix parameters
    let helix = PyDict::new_bound(py);
    helix.set_item("radius", result.helix_radius)?;
    helix.set_item("pitch", result.helix_pitch)?;
    helix.set_item("height", result.helix_height)?;
    helix.set_item("turns", result.helix_turns)?;
    d.set_item("helix_params", helix)?;

    // Analysis
    let analysis = PyDict::new_bound(py);
    analysis.set_item("spring_rate", result.spring_rate)?;
    analysis.set_item("wahl_factor", result.wahl_factor)?;
    analysis.set_item("solid_height", result.solid_height)?;
    analysis.set_item("max_deflection", result.max_deflection)?;
    analysis.set_item("natural_freq_hz", result.natural_freq_hz)?;
    analysis.set_item("buckling_critical", result.buckling_critical)?;
    analysis.set_item("max_shear_stress_mpa", result.max_shear_stress_mpa)?;
    analysis.set_item("stress_at_solid_mpa", result.stress_at_solid_mpa)?;
    if let Some(ok) = result.stress_ok {
        analysis.set_item("stress_ok", ok)?;
    }
    analysis.set_item("spring_type", &result.spring_type)?;
    analysis.set_item("end_type", &result.end_type)?;
    d.set_item("analysis", analysis)?;

    d.set_item("build_hint",
        "1. cad.helix(radius=helix_radius, pitch=helix_pitch, height=helix_height)\n\
         2. cad.sketch(geometry_ref=wire_ref) on helix start plane\n\
         3. cad.sweep(profile=sketch, path=helix)")?;
    Ok(d.unbind().into())
}

/// Generate a cam profile from motion law segments.
#[pyfunction]
#[pyo3(signature = (
    base_radius, segments, follower_type = "knife_edge",
    follower_radius = 0.0, center_x = 0.0, center_y = 0.0,
    num_points_per_segment = 50
))]
fn cam_profile(
    py: Python<'_>, base_radius: f64, segments: &Bound<'_, PyList>,
    follower_type: &str, follower_radius: f64,
    center_x: f64, center_y: f64, num_points_per_segment: usize,
) -> PyResult<PyObject> {
    // Parse segments from Python list of dicts
    let mut segs: Vec<CamSegment> = Vec::new();
    for item in segments.iter() {
        let dict = item.downcast::<PyDict>()?;
        let start = dict.get_item("start_angle_deg")?.ok_or_else(||
            pyo3::exceptions::PyValueError::new_err("segment missing start_angle_deg")
        )?.extract::<f64>()?;
        let end = dict.get_item("end_angle_deg")?.ok_or_else(||
            pyo3::exceptions::PyValueError::new_err("segment missing end_angle_deg")
        )?.extract::<f64>()?;
        let rise = dict.get_item("rise_mm")?.ok_or_else(||
            pyo3::exceptions::PyValueError::new_err("segment missing rise_mm")
        )?.extract::<f64>()?;
        let law: String = dict.get_item("motion_law")?.ok_or_else(||
            pyo3::exceptions::PyValueError::new_err("segment missing motion_law")
        )?.extract()?;
        segs.push(CamSegment {
            start_angle_deg: start,
            end_angle_deg: end,
            rise_mm: rise,
            motion_law: law,
        });
    }

    let result = cam_profile_impl(
        base_radius, &segs, follower_type, follower_radius,
        center_x, center_y, num_points_per_segment,
    ).map_err(|e| pyo3::exceptions::PyValueError::new_err(e))?;

    let d = PyDict::new_bound(py);
    let elements = PyList::empty_bound(py);
    for elem in &result.profile.elements {
        elements.append(sketch_element_to_py(py, elem)?)?;
    }
    d.set_item("elements", elements)?;
    d.set_item("max_pressure_angle_deg", result.max_pressure_angle_deg)?;
    d.set_item("max_acceleration", result.max_acceleration)?;

    // Displacement curve
    let disp_list = PyList::empty_bound(py);
    for pt in &result.displacement_curve {
        disp_list.append(vec![pt[0], pt[1]])?;
    }
    d.set_item("displacement_curve", disp_list)?;

    d.set_item("build_hint",
        "1. cad.sketch(geometry_ref=...) with the cam outline\n\
         2. cad.pad to desired cam thickness\n\
         3. cad.hole for the bore")?;
    Ok(d.unbind().into())
}

// ---------------------------------------------------------------------------
// Module registration
// ---------------------------------------------------------------------------

#[pymodule]
fn solidmind_geometry(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Involute gear tools
    m.add_function(wrap_pyfunction!(spur_gear, m)?)?;
    m.add_function(wrap_pyfunction!(tooth_slot, m)?)?;
    m.add_function(wrap_pyfunction!(gear_params, m)?)?;
    m.add_function(wrap_pyfunction!(involute_points, m)?)?;
    m.add_function(wrap_pyfunction!(planetary_layout, m)?)?;
    m.add_function(wrap_pyfunction!(propeller_blade_py, m)?)?;
    // Generalized tools
    m.add_function(wrap_pyfunction!(epicycloidal_tooth_slot, m)?)?;
    m.add_function(wrap_pyfunction!(spiral_py, m)?)?;
    m.add_function(wrap_pyfunction!(spoke_pattern_py, m)?)?;
    // Phase 2: Gear extensions
    m.add_function(wrap_pyfunction!(bevel_gear, m)?)?;
    m.add_function(wrap_pyfunction!(worm_gear, m)?)?;
    m.add_function(wrap_pyfunction!(thread_profile, m)?)?;
    // Phase 3: Complex mechanisms
    m.add_function(wrap_pyfunction!(helical_spring, m)?)?;
    m.add_function(wrap_pyfunction!(cam_profile, m)?)?;
    Ok(())
}
