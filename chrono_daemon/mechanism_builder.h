// mechanism_builder.h — Build Chrono multibody system from JSON mechanism definition.

#pragma once

#include <nlohmann/json.hpp>
#include <chrono/physics/ChSystemNSC.h>
#include <chrono/physics/ChBody.h>
#include <chrono/physics/ChLinkGear.h>
#include <chrono/physics/ChLinkLockRevolute.h>
#include <chrono/physics/ChLinkLockPrismatic.h>
#include <chrono/physics/ChLinkLockLock.h>
#include <chrono/physics/ChLinkMotorRotationSpeed.h>
#include <chrono/motion_functions/ChFunction_Const.h>
#include <string>
#include <unordered_map>
#include <memory>
#include <cmath>

using json = nlohmann::json;

namespace chrono_daemon {

/// Result of building a mechanism in Chrono.
struct BuiltMechanism {
    std::shared_ptr<chrono::ChSystemNSC> system;
    std::unordered_map<std::string, std::shared_ptr<chrono::ChBody>> bodies;
    std::unordered_map<std::string, std::shared_ptr<chrono::ChLinkMotorRotationSpeed>> motors;
    std::vector<std::string> warnings;
};

/// Build a Chrono system from a JSON mechanism definition.
///
/// The mechanism JSON follows the same schema as the Python Mechanism.to_dict():
///   {
///     "parts": [{"id": "sun", "mass_kg": 0.1, "inertia_kg_m2": 0.001, "is_ground": false, ...}],
///     "joints": [{"id": "mesh1", "joint_type": "gear_mesh", "parent_part": "sun",
///                  "child_part": "planet", "gear_ratio": 2.0, ...}],
///     "drives": [{"joint_id": "mesh1", "speed_rpm": 1000, "torque_nm": 5.0}]
///   }
inline BuiltMechanism build_mechanism(const json& mechanism_json) {
    BuiltMechanism result;
    result.system = std::make_shared<chrono::ChSystemNSC>();
    result.system->Set_G_acc(chrono::ChVector<>(0, 0, 0));  // No gravity for gear validation

    auto& sys = *result.system;

    // 1. Create bodies
    for (const auto& part_json : mechanism_json["parts"]) {
        std::string id = part_json["id"];
        bool is_ground = part_json.value("is_ground", false);

        if (is_ground) {
            // Use the system's ground body
            auto ground = sys.GetGroundBody();
            result.bodies[id] = ground;
            continue;
        }

        auto body = chrono_types::make_shared<chrono::ChBody>();
        body->SetName(id);

        double mass = part_json.value("mass_kg", 1.0);
        double inertia = part_json.value("inertia_kg_m2", 0.01);
        body->SetMass(mass);
        body->SetInertiaXX(chrono::ChVector<>(inertia, inertia, inertia));

        // Position at origin (joints define relative placement)
        auto origin = part_json.value("origin", std::vector<double>{0, 0, 0});
        if (origin.size() >= 3) {
            body->SetPos(chrono::ChVector<>(origin[0] / 1000.0, origin[1] / 1000.0, origin[2] / 1000.0));
        }

        body->SetBodyFixed(false);
        sys.AddBody(body);
        result.bodies[id] = body;
    }

    // 2. Create joints
    for (const auto& joint_json : mechanism_json["joints"]) {
        std::string joint_id = joint_json["id"];
        std::string joint_type = joint_json["joint_type"];
        std::string parent_id = joint_json["parent_part"];
        std::string child_id = joint_json["child_part"];

        auto parent_it = result.bodies.find(parent_id);
        auto child_it = result.bodies.find(child_id);
        if (parent_it == result.bodies.end() || child_it == result.bodies.end()) {
            result.warnings.push_back("Joint '" + joint_id + "' references unknown part");
            continue;
        }
        auto& parent_body = parent_it->second;
        auto& child_body = child_it->second;

        auto axis_vec = joint_json.value("axis", std::vector<double>{0, 0, 1});
        auto origin_vec = joint_json.value("origin", std::vector<double>{0, 0, 0});
        chrono::ChVector<> axis(axis_vec[0], axis_vec[1], axis_vec[2]);
        chrono::ChVector<> origin(origin_vec[0] / 1000.0, origin_vec[1] / 1000.0, origin_vec[2] / 1000.0);

        if (joint_type == "gear_mesh") {
            auto gear = chrono_types::make_shared<chrono::ChLinkGear>();

            double ratio = joint_json.value("gear_ratio", 1.0);
            // If teeth are provided, compute ratio from them
            if (joint_json.contains("teeth_parent") && joint_json.contains("teeth_child")) {
                int tp = joint_json["teeth_parent"];
                int tc = joint_json["teeth_child"];
                if (tc > 0) ratio = static_cast<double>(tp) / tc;
            }

            // Approximate radii for constraint setup (module=1mm default)
            double rad_parent = 0.01;  // 10mm
            double rad_child = 0.01 / ratio;
            if (joint_json.contains("teeth_parent")) {
                rad_parent = joint_json["teeth_parent"].get<double>() * 0.001 / 2.0;
            }
            if (joint_json.contains("teeth_child")) {
                rad_child = joint_json["teeth_child"].get<double>() * 0.001 / 2.0;
            }

            // Set gear constraint frames
            chrono::ChFrame<> frame_parent(parent_body->GetPos(), chrono::Q_from_AngZ(0));
            chrono::ChFrame<> frame_child(child_body->GetPos(), chrono::Q_from_AngZ(0));

            gear->Initialize(parent_body, child_body, false,
                             frame_parent, frame_child);
            gear->Set_tau(ratio);
            gear->Set_epicyclic(false);  // Default: external mesh

            // Check for internal gear (ring gear)
            if (joint_json.value("internal", false)) {
                gear->Set_epicyclic(true);
            }

            sys.AddLink(gear);

        } else if (joint_type == "revolute") {
            auto revolute = chrono_types::make_shared<chrono::ChLinkLockRevolute>();
            chrono::ChCoordsys<> csys(origin, chrono::QUNIT);
            revolute->Initialize(parent_body, child_body, csys);
            sys.AddLink(revolute);

        } else if (joint_type == "prismatic") {
            auto prismatic = chrono_types::make_shared<chrono::ChLinkLockPrismatic>();
            chrono::ChCoordsys<> csys(origin, chrono::QUNIT);
            prismatic->Initialize(parent_body, child_body, csys);
            sys.AddLink(prismatic);

        } else if (joint_type == "fixed") {
            auto fixed = chrono_types::make_shared<chrono::ChLinkLockLock>();
            chrono::ChCoordsys<> csys(origin, chrono::QUNIT);
            fixed->Initialize(parent_body, child_body, csys);
            sys.AddLink(fixed);

        } else {
            result.warnings.push_back("Unsupported joint type: " + joint_type + " for joint " + joint_id);
        }
    }

    // 3. Create motors from drives
    for (const auto& drive_json : mechanism_json.value("drives", json::array())) {
        std::string joint_id = drive_json["joint_id"];

        // Find the joint's parent part to attach the motor
        for (const auto& joint_json : mechanism_json["joints"]) {
            if (joint_json["id"] == joint_id) {
                std::string parent_id = joint_json["parent_part"];
                auto parent_it = result.bodies.find(parent_id);
                if (parent_it == result.bodies.end()) break;

                if (drive_json.contains("speed_rpm") && !drive_json["speed_rpm"].is_null()) {
                    double rpm = drive_json["speed_rpm"];
                    double omega = rpm * 2.0 * M_PI / 60.0;

                    auto motor = chrono_types::make_shared<chrono::ChLinkMotorRotationSpeed>();
                    motor->SetName(joint_id + "_motor");
                    motor->SetSpeedFunction(
                        chrono_types::make_shared<chrono::ChFunction_Const>(omega));

                    // Attach motor between ground and the driven part
                    auto ground = sys.GetGroundBody();
                    chrono::ChFrame<> frame(parent_it->second->GetPos());
                    motor->Initialize(parent_it->second, ground, frame);
                    sys.AddLink(motor);
                    result.motors[joint_id] = motor;
                }
                break;
            }
        }
    }

    return result;
}

} // namespace chrono_daemon
