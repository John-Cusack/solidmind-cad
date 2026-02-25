mod gears;
mod involute;
mod planetary;
mod propeller;
mod types;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use gears::{
    compute_gear_params, compute_internal_gear_params, internal_gear_profile, single_tooth_slot,
    spur_gear_profile,
};
use involute::involute_curve_points;
use planetary::planetary_layout as planetary_layout_impl;
use types::{GearParams, SketchElement, SketchResult};

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
        SketchElement::Arc {
            cx,
            cy,
            r,
            start_angle,
            end_angle,
        } => {
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
        SketchElement::Spline {
            points,
            degree,
            periodic,
            weights,
        } => {
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

// ---------------------------------------------------------------------------
// PyO3-exported functions
// ---------------------------------------------------------------------------

/// Generate a spur gear profile (external or internal).
///
/// Returns a dict with 'elements' (list of cad.sketch element dicts) and 'params'.
#[pyfunction]
#[pyo3(signature = (
    module,
    teeth,
    pressure_angle_deg = 20.0,
    clearance_coeff = 0.25,
    profile_shift = 0.0,
    backlash = 0.0,
    center_x = 0.0,
    center_y = 0.0,
    num_involute_pts = 20,
    internal = false
))]
fn spur_gear(
    py: Python<'_>,
    module: f64,
    teeth: u32,
    pressure_angle_deg: f64,
    clearance_coeff: f64,
    profile_shift: f64,
    backlash: f64,
    center_x: f64,
    center_y: f64,
    num_involute_pts: usize,
    internal: bool,
) -> PyResult<PyObject> {
    let center = [center_x, center_y];

    let (params, result) = if internal {
        let p = compute_internal_gear_params(
            module,
            teeth,
            pressure_angle_deg,
            clearance_coeff,
            profile_shift,
            backlash,
        );
        let r = internal_gear_profile(&p, center, num_involute_pts);
        (p, r)
    } else {
        let p = compute_gear_params(
            module,
            teeth,
            pressure_angle_deg,
            clearance_coeff,
            profile_shift,
            backlash,
        );
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

    // Build hint for the LLM
    d.set_item(
        "build_hint",
        if internal {
            "Use these elements in a cad.sketch, then cad.pad for the ring gear body. \
             You may need a separate outer circle for the ring housing."
        } else {
            "Use these elements in a cad.sketch, then cad.pad to extrude the gear. \
             Add a center bore with cad.hole if needed."
        },
    )?;

    Ok(d.unbind().into())
}

/// Generate a single tooth slot profile for pocket + polar pattern workflow.
#[pyfunction]
#[pyo3(signature = (
    module,
    teeth,
    pressure_angle_deg = 20.0,
    clearance_coeff = 0.25,
    profile_shift = 0.0,
    backlash = 0.0,
    center_x = 0.0,
    center_y = 0.0,
    num_involute_pts = 20
))]
fn tooth_slot(
    py: Python<'_>,
    module: f64,
    teeth: u32,
    pressure_angle_deg: f64,
    clearance_coeff: f64,
    profile_shift: f64,
    backlash: f64,
    center_x: f64,
    center_y: f64,
    num_involute_pts: usize,
) -> PyResult<PyObject> {
    let params = compute_gear_params(
        module,
        teeth,
        pressure_angle_deg,
        clearance_coeff,
        profile_shift,
        backlash,
    );
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
    d.set_item(
        "build_hint",
        "1. Create a blank cylinder: cad.sketch (circle, r=tip_diameter/2) → cad.pad\n\
         2. cad.sketch with these elements → cad.pocket (ThroughAll)\n\
         3. cad.polar_pattern(features=['Pocket'], occurrences=teeth)",
    )?;
    Ok(d.unbind().into())
}

/// Compute gear parameters without generating geometry.
#[pyfunction]
#[pyo3(signature = (
    module,
    teeth,
    pressure_angle_deg = 20.0,
    clearance_coeff = 0.25,
    profile_shift = 0.0,
    backlash = 0.0,
    internal = false
))]
fn gear_params(
    py: Python<'_>,
    module: f64,
    teeth: u32,
    pressure_angle_deg: f64,
    clearance_coeff: f64,
    profile_shift: f64,
    backlash: f64,
    internal: bool,
) -> PyResult<PyObject> {
    let p = if internal {
        compute_internal_gear_params(
            module,
            teeth,
            pressure_angle_deg,
            clearance_coeff,
            profile_shift,
            backlash,
        )
    } else {
        compute_gear_params(
            module,
            teeth,
            pressure_angle_deg,
            clearance_coeff,
            profile_shift,
            backlash,
        )
    };
    gear_params_to_py(py, &p)
}

/// Generate points along an involute curve.
#[pyfunction]
#[pyo3(signature = (base_radius, start_radius, end_radius, num_points = 20))]
fn involute_points(
    base_radius: f64,
    start_radius: f64,
    end_radius: f64,
    num_points: usize,
) -> Vec<(f64, f64)> {
    involute_curve_points(base_radius, start_radius, end_radius, num_points)
        .into_iter()
        .map(|p| (p[0], p[1]))
        .collect()
}

/// Generate a complete planetary gear layout.
///
/// Returns a dict with sun, planet, ring profiles and planet_positions.
#[pyfunction]
#[pyo3(signature = (
    module,
    sun_teeth,
    planet_teeth,
    num_planets = 3,
    pressure_angle_deg = 20.0,
    clearance_coeff = 0.25,
    profile_shift = 0.0,
    backlash = 0.0,
    center_x = 0.0,
    center_y = 0.0,
    num_involute_pts = 20
))]
fn planetary_layout(
    py: Python<'_>,
    module: f64,
    sun_teeth: u32,
    planet_teeth: u32,
    num_planets: u32,
    pressure_angle_deg: f64,
    clearance_coeff: f64,
    profile_shift: f64,
    backlash: f64,
    center_x: f64,
    center_y: f64,
    num_involute_pts: usize,
) -> PyResult<PyObject> {
    let center = [center_x, center_y];
    let layout = planetary_layout_impl(
        module,
        sun_teeth,
        planet_teeth,
        num_planets,
        pressure_angle_deg,
        clearance_coeff,
        profile_shift,
        backlash,
        center,
        num_involute_pts,
    )
    .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

    let d = PyDict::new_bound(py);
    d.set_item("sun", sketch_result_to_py(py, &layout.sun)?)?;
    d.set_item("planet", sketch_result_to_py(py, &layout.planet)?)?;
    d.set_item("ring", sketch_result_to_py(py, &layout.ring)?)?;
    d.set_item("planet_positions", layout.planet_positions.to_vec())?;

    let params = PyDict::new_bound(py);
    params.set_item("sun", gear_params_to_py(py, &layout.sun_params)?)?;
    params.set_item("planet", gear_params_to_py(py, &layout.planet_params)?)?;
    params.set_item("ring", gear_params_to_py(py, &layout.ring_params)?)?;
    params.set_item("ring_teeth", layout.ring_params.teeth)?;
    d.set_item("params", params)?;

    Ok(d.unbind().into())
}

/// Generate a propeller blade definition with airfoil sections, blade table,
/// and Selig-format .dat string.
#[pyfunction]
#[pyo3(signature = (
    diameter,
    pitch,
    hub_diameter,
    num_blades = 2,
    airfoil = "NACA4412",
    chord_root = None,
    chord_tip = None,
    num_sections = 6,
    num_points = 40
))]
fn propeller_blade_py(
    py: Python<'_>,
    diameter: f64,
    pitch: f64,
    hub_diameter: f64,
    num_blades: u32,
    airfoil: &str,
    chord_root: Option<f64>,
    chord_tip: Option<f64>,
    num_sections: usize,
    num_points: usize,
) -> PyResult<PyObject> {
    let result = propeller::propeller_blade(
        diameter,
        pitch,
        hub_diameter,
        num_blades,
        airfoil,
        chord_root,
        chord_tip,
        num_sections,
        num_points,
    )
    .map_err(|e| pyo3::exceptions::PyValueError::new_err(e))?;

    let d = PyDict::new_bound(py);

    // sections: list of dicts with elements + metadata
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

    // hub
    let hub_dict = PyDict::new_bound(py);
    let hub_elements = PyList::empty_bound(py);
    for elem in &result.hub.elements {
        hub_elements.append(sketch_element_to_py(py, elem)?)?;
    }
    hub_dict.set_item("elements", hub_elements)?;
    hub_dict.set_item("diameter_mm", result.hub_diameter_mm)?;
    hub_dict.set_item("height_mm", result.hub_height_mm)?;
    d.set_item("hub", hub_dict)?;

    // blade_table
    let bt = PyDict::new_bound(py);
    bt.set_item("r_frac", result.blade_table.r_frac.clone())?;
    bt.set_item("chord_mm", result.blade_table.chord_mm.clone())?;
    bt.set_item("twist_deg", result.blade_table.twist_deg.clone())?;
    bt.set_item("Re_at_5000rpm", result.blade_table.re_at_5000rpm.clone())?;
    d.set_item("blade_table", bt)?;

    // airfoil_dat
    d.set_item("airfoil_dat", &result.airfoil_dat)?;

    // params
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
// Module registration
// ---------------------------------------------------------------------------

#[pymodule]
fn solidmind_geometry(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(spur_gear, m)?)?;
    m.add_function(wrap_pyfunction!(tooth_slot, m)?)?;
    m.add_function(wrap_pyfunction!(gear_params, m)?)?;
    m.add_function(wrap_pyfunction!(involute_points, m)?)?;
    m.add_function(wrap_pyfunction!(planetary_layout, m)?)?;
    m.add_function(wrap_pyfunction!(propeller_blade_py, m)?)?;
    Ok(())
}
