// simulator.h — Time-stepping loop and result collection for Chrono simulations.
//
// Handles both 1D shaft elements and 3D body elements.  The BuiltMechanism
// struct may contain shafts, bodies, or both.

#pragma once

#include <nlohmann/json.hpp>
#include "mechanism_builder.h"
#include <chrono/physics/ChSystemNSC.h>
#include <cmath>
#include <string>
#include <vector>

using json = nlohmann::json;

namespace chrono_daemon {

/// Run a time-domain simulation and collect results.
///
/// Returns JSON with:
///   {
///     "time_series": [{"t": 0.0, "parts": {"sun": {"omega_rpm": ...}}}],
///     "summary": {
///       "steady_state_speeds": {"sun": 1000, "carrier": 333.3},
///       "peak_torques": {"sun_motor": 5.2},
///       "efficiency_model": "none"
///     }
///   }
inline json run_simulation(
    BuiltMechanism& mech,
    double duration_s,
    double dt_s,
    double output_interval
) {
    auto& sys = *mech.system;
    json time_series = json::array();

    std::unordered_map<std::string, double> peak_torques;
    std::unordered_map<std::string, double> last_speeds;

    double next_output = 0.0;
    double t = 0.0;

    while (t <= duration_s + dt_s * 0.5) {
        sys.DoStepDynamics(dt_s);
        t += dt_s;

        if (t >= next_output - dt_s * 0.01) {
            json step;
            step["t"] = std::round(t * 10000.0) / 10000.0;
            json parts_state;

            // Collect shaft states (1D elements)
            for (auto& [id, shaft] : mech.shafts) {
                // GetPosDt() returns angular velocity in rad/s
                double omega_rad_s = shaft->GetPosDt();
                double rpm = omega_rad_s * 60.0 / (2.0 * M_PI);
                double angle_rad = shaft->GetPos();

                parts_state[id] = {
                    {"pos", {0, 0, 0}},
                    {"rot", {1, 0, 0, 0}},
                    {"omega_rpm", std::round(rpm * 100.0) / 100.0},
                    {"angle_rad", std::round(angle_rad * 1000.0) / 1000.0}
                };

                last_speeds[id] = rpm;
            }

            // Collect body states (3D elements)
            for (auto& [id, body] : mech.bodies) {
                auto pos = body->GetPos();
                auto rot = body->GetRot();
                auto wvel = body->GetAngVelLocal();

                double omega_rad_s = wvel.z();
                double rpm = omega_rad_s * 60.0 / (2.0 * M_PI);

                parts_state[id] = {
                    {"pos", {pos.x(), pos.y(), pos.z()}},
                    {"rot", {rot.e0(), rot.e1(), rot.e2(), rot.e3()}},
                    {"omega_rpm", std::round(rpm * 100.0) / 100.0}
                };

                last_speeds[id] = rpm;
            }

            // Collect shaft motor torques
            for (auto& [motor_id, motor] : mech.shaft_motors) {
                double torque = motor->GetMotorLoad();
                double abs_torque = std::abs(torque);
                if (abs_torque > peak_torques[motor_id]) {
                    peak_torques[motor_id] = abs_torque;
                }
            }

            // Collect body motor torques
            for (auto& [motor_id, motor] : mech.body_motors) {
                double torque = motor->GetMotorTorque();
                double abs_torque = std::abs(torque);
                if (abs_torque > peak_torques[motor_id]) {
                    peak_torques[motor_id] = abs_torque;
                }
            }

            step["parts"] = parts_state;
            time_series.push_back(step);
            next_output += output_interval;
        }
    }

    // Build summary
    json steady_state_speeds;
    for (auto& [id, rpm] : last_speeds) {
        steady_state_speeds[id] = std::round(rpm * 100.0) / 100.0;
    }

    json peak_torques_json;
    for (auto& [id, torque] : peak_torques) {
        peak_torques_json[id] = std::round(torque * 1000.0) / 1000.0;
    }

    int output_samples = static_cast<int>(time_series.size());

    json summary = {
        {"steady_state_speeds", steady_state_speeds},
        {"peak_torques", peak_torques_json},
        {"efficiency_model", "none"},
        {"efficiency_note", "1D shaft model — no friction losses modeled"},
        {"simulation_time_s", duration_s},
        {"time_steps", static_cast<int>(duration_s / dt_s)},
        {"output_samples", output_samples}
    };

    return {
        {"time_series", time_series},
        {"summary", summary}
    };
}

} // namespace chrono_daemon
