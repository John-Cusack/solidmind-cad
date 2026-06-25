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

    // Joint reaction accumulators — one per stored link.
    std::unordered_map<std::string, JointReactionAccum> joint_reactions;
    for (auto& kv : mech.links) {
        joint_reactions[kv.first] = JointReactionAccum{kv.first, 0.0, 0.0, 0};
    }

    // Applied-force time-series capture (for thrust_mean / thrust_std).
    // We accumulate the world-frame Z component of every applied force at each
    // output sample so the Python side can derive aggregate metrics.
    std::vector<double> applied_force_world_z_at_sample;

    double next_output = 0.0;
    double t = 0.0;

    while (t <= duration_s + dt_s * 0.5) {
        // Apply external forces (e.g. BEMT distributed loads). Refresh each step
        // so the world-frame application point and orientation track body motion.
        for (auto& af : mech.applied_forces) {
            auto body_it = mech.bodies.find(af.body_id);
            if (body_it == mech.bodies.end()) continue;
            auto& body = body_it->second;
            body->EmptyAccumulator(af.accumulator_idx);
            if (af.world_frame) {
                // World-frame force at body-anchored application point.
                chrono::ChVector3d world_pos =
                    body->TransformPointLocalToParent(af.position_local);
                body->AccumulateForce(
                    af.accumulator_idx, af.force_vector, world_pos, false);
            } else {
                // Both force and point in body-local frame (force rotates with body).
                body->AccumulateForce(
                    af.accumulator_idx, af.force_vector, af.position_local, true);
            }
        }

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

            // Sample joint reactions (force magnitude on body 1, in link frame).
            for (auto& [link_id, link] : mech.links) {
                auto wrench = link->GetReaction1();
                double mag = wrench.force.Length();
                auto& acc = joint_reactions[link_id];
                acc.sum_force_mag += mag;
                if (mag > acc.peak_force_mag) acc.peak_force_mag = mag;
                acc.samples += 1;
            }

            // Sample summed world-frame Z component of all applied forces.
            // For body-frame forces we transform to world here; for world-frame
            // forces we use the stored vector directly.
            double total_world_z = 0.0;
            for (auto& af : mech.applied_forces) {
                auto body_it = mech.bodies.find(af.body_id);
                if (body_it == mech.bodies.end()) continue;
                if (af.world_frame) {
                    total_world_z += af.force_vector.z();
                } else {
                    chrono::ChVector3d world_force =
                        body_it->second->TransformDirectionLocalToParent(af.force_vector);
                    total_world_z += world_force.z();
                }
            }
            applied_force_world_z_at_sample.push_back(total_world_z);

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

    // Joint-reaction summary: peak and mean magnitudes.
    json peak_joint_forces = json::object();
    json mean_joint_forces = json::object();
    for (auto& [id, acc] : joint_reactions) {
        peak_joint_forces[id] = std::round(acc.peak_force_mag * 1000.0) / 1000.0;
        double mean = (acc.samples > 0) ? (acc.sum_force_mag / acc.samples) : 0.0;
        mean_joint_forces[id] = std::round(mean * 1000.0) / 1000.0;
    }

    // Applied-force aggregate (mean and std of world-Z over the run).
    double mean_z = 0.0, std_z = 0.0;
    if (!applied_force_world_z_at_sample.empty()) {
        for (double v : applied_force_world_z_at_sample) mean_z += v;
        mean_z /= applied_force_world_z_at_sample.size();
        for (double v : applied_force_world_z_at_sample) {
            std_z += (v - mean_z) * (v - mean_z);
        }
        std_z = std::sqrt(std_z / applied_force_world_z_at_sample.size());
    }

    json summary = {
        {"steady_state_speeds", steady_state_speeds},
        {"peak_torques", peak_torques_json},
        {"peak_joint_forces", peak_joint_forces},
        {"mean_joint_forces", mean_joint_forces},
        {"applied_force_world_z_mean_N", std::round(mean_z * 1000.0) / 1000.0},
        {"applied_force_world_z_std_N", std::round(std_z * 1000.0) / 1000.0},
        {"applied_force_count", static_cast<int>(mech.applied_forces.size())},
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
