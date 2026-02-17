// mechanism_builder.h — Generic Chrono object factory from simulation spec JSON.
//
// The Python planner (simulation_spec_builder.py) decides WHAT Chrono elements
// to create.  This C++ code just instantiates them — no domain logic, no gear
// ratio computation, no planetary detection.
//
// Supported object types:
//   shaft               → ChShaft (1D rotational inertia)
//   body                → ChBody (3D rigid body)
//   shafts_gear         → ChShaftsGear (1D gear constraint between two shafts)
//   shafts_planetary    → ChShaftsPlanetary (Willis epicyclic constraint)
//   shaft_body_rotation → ChShaftBodyRotation (couples shaft to body Z-axis)
//   revolute            → ChLinkLockRevolute (3D revolute joint)
//   prismatic           → ChLinkLockPrismatic (3D prismatic joint)
//   fixed               → ChLinkLockLock (3D rigid joint)
//   motor_shaft_speed   → ChShaftsMotorSpeed (constant-speed motor on a shaft)
//   motor_body_speed    → ChLinkMotorRotationSpeed (constant-speed motor on a body)

#pragma once

#include <nlohmann/json.hpp>
#include <chrono/physics/ChSystemNSC.h>
#include <chrono/physics/ChBody.h>
#include <chrono/physics/ChShaft.h>
#include <chrono/physics/ChShaftsGear.h>
#include <chrono/physics/ChShaftsPlanetary.h>
#include <chrono/physics/ChShaftsMotorSpeed.h>
#include <chrono/physics/ChShaftBodyConstraint.h>
#include <chrono/physics/ChLinkLock.h>
#include <chrono/physics/ChLinkLockGear.h>
#include <chrono/physics/ChLinkMotorRotationSpeed.h>
#include <chrono/core/ChRotation.h>
#include <chrono/functions/ChFunctionConst.h>
#include <chrono/solver/ChIterativeSolverVI.h>
#include <string>
#include <unordered_map>
#include <memory>
#include <cmath>

using json = nlohmann::json;

namespace chrono_daemon {

/// Safe accessor: returns default if key is missing OR null.
template<typename T>
T json_get(const json& j, const std::string& key, const T& default_val) {
    if (j.contains(key) && !j[key].is_null()) {
        return j[key].get<T>();
    }
    return default_val;
}

/// Result of building a mechanism in Chrono.
struct BuiltMechanism {
    std::shared_ptr<chrono::ChSystemNSC> system;
    // 1D shaft elements (keyed by part id)
    std::unordered_map<std::string, std::shared_ptr<chrono::ChShaft>> shafts;
    // 3D body elements (keyed by part id)
    std::unordered_map<std::string, std::shared_ptr<chrono::ChBody>> bodies;
    // Motors — both shaft and body types
    std::unordered_map<std::string, std::shared_ptr<chrono::ChShaftsMotorSpeed>> shaft_motors;
    std::unordered_map<std::string, std::shared_ptr<chrono::ChLinkMotorRotationSpeed>> body_motors;
    std::vector<std::string> warnings;
};

/// Build a Chrono system from a simulation spec (new Python-planner path).
///
/// The spec is a flat list of objects:
///   {"objects": [{type, id, ...}, ...], "derived_outputs": {...}}
///
/// Each object type maps to exactly one Chrono class — no interpretation needed.
inline BuiltMechanism build_mechanism_from_spec(const json& spec_json) {
    BuiltMechanism result;
    result.system = std::make_shared<chrono::ChSystemNSC>();
    result.system->SetGravitationalAcceleration(chrono::ChVector3d(0, 0, 0));
    result.system->SetMaxPenetrationRecoverySpeed(1.0);
    if (auto solver = std::dynamic_pointer_cast<chrono::ChIterativeSolverVI>(result.system->GetSolver())) {
        solver->SetMaxIterations(150);
    }

    auto& sys = *result.system;

    // Ground body for anchoring fixed shafts and body motors
    auto ground_body = chrono_types::make_shared<chrono::ChBody>();
    ground_body->SetName("__ground__");
    ground_body->SetFixed(true);
    ground_body->SetMass(1.0);
    sys.AddBody(ground_body);

    for (const auto& obj : spec_json["objects"]) {
        std::string type = obj["type"];
        std::string id = obj["id"];

        if (type == "shaft") {
            auto shaft = chrono_types::make_shared<chrono::ChShaft>();
            shaft->SetName(id);
            double inertia = json_get(obj, "inertia", 0.01);
            shaft->SetInertia(inertia);
            bool fixed = json_get(obj, "fixed", false);
            shaft->SetFixed(fixed);
            sys.AddShaft(shaft);
            result.shafts[id] = shaft;

        } else if (type == "body") {
            auto body = chrono_types::make_shared<chrono::ChBody>();
            body->SetName(id);
            double mass = json_get(obj, "mass", 1.0);
            double inertia = json_get(obj, "inertia", 0.01);
            body->SetMass(mass);
            body->SetInertiaXX(chrono::ChVector3d(inertia, inertia, inertia));
            bool fixed = json_get(obj, "fixed", false);
            body->SetFixed(fixed);

            auto pos_vec = json_get(obj, "pos", std::vector<double>{0, 0, 0});
            body->SetPos(chrono::ChVector3d(pos_vec[0], pos_vec[1], pos_vec[2]));

            sys.AddBody(body);
            result.bodies[id] = body;

        } else if (type == "shafts_gear") {
            auto s1_it = result.shafts.find(obj["shaft_1"].get<std::string>());
            auto s2_it = result.shafts.find(obj["shaft_2"].get<std::string>());
            if (s1_it == result.shafts.end() || s2_it == result.shafts.end()) {
                result.warnings.push_back("shafts_gear '" + id + "': shaft not found");
                continue;
            }
            auto gear = chrono_types::make_shared<chrono::ChShaftsGear>();
            gear->SetName(id);
            double ratio = json_get(obj, "ratio", -1.0);
            gear->SetTransmissionRatio(ratio);
            gear->Initialize(s1_it->second, s2_it->second);
            sys.Add(gear);

        } else if (type == "shafts_planetary") {
            std::string sun_id = obj["shaft_sun"];
            std::string carrier_id = obj["shaft_carrier"];
            std::string ring_id = obj["shaft_ring"];
            auto sun_it = result.shafts.find(sun_id);
            auto carrier_it = result.shafts.find(carrier_id);
            auto ring_it = result.shafts.find(ring_id);
            if (sun_it == result.shafts.end() ||
                carrier_it == result.shafts.end() ||
                ring_it == result.shafts.end()) {
                result.warnings.push_back("shafts_planetary '" + id + "': shaft not found");
                continue;
            }
            auto planetary = chrono_types::make_shared<chrono::ChShaftsPlanetary>();
            planetary->SetName(id);
            double t0 = json_get(obj, "t0", -0.5);
            planetary->Initialize(
                carrier_it->second,  // carrier (shaft 1)
                sun_it->second,      // sun (shaft 2, input)
                ring_it->second      // ring (shaft 3, annulus)
            );
            planetary->SetTransmissionRatioOrdinary(t0);
            sys.Add(planetary);

        } else if (type == "shaft_body_rotation") {
            auto shaft_it = result.shafts.find(obj["shaft"].get<std::string>());
            auto body_it = result.bodies.find(obj["body"].get<std::string>());
            if (shaft_it == result.shafts.end() || body_it == result.bodies.end()) {
                result.warnings.push_back("shaft_body_rotation '" + id + "': element not found");
                continue;
            }
            auto sbr = chrono_types::make_shared<chrono::ChShaftBodyRotation>();
            sbr->SetName(id);
            // Rotation axis (default Z)
            auto axis_vec = json_get(obj, "axis", std::vector<double>{0, 0, 1});
            chrono::ChVector3d axis(axis_vec[0], axis_vec[1], axis_vec[2]);
            sbr->Initialize(shaft_it->second, body_it->second, axis);
            sys.Add(sbr);

        } else if (type == "revolute") {
            auto b1_it = result.bodies.find(obj["body_1"].get<std::string>());
            auto b2_it = result.bodies.find(obj["body_2"].get<std::string>());
            if (b1_it == result.bodies.end() || b2_it == result.bodies.end()) {
                result.warnings.push_back("revolute '" + id + "': body not found");
                continue;
            }
            auto pos_vec = json_get(obj, "pos", std::vector<double>{0, 0, 0});
            chrono::ChVector3d pos(pos_vec[0], pos_vec[1], pos_vec[2]);
            auto revolute = chrono_types::make_shared<chrono::ChLinkLockRevolute>();
            chrono::ChFrame<> frame(pos);
            revolute->Initialize(b1_it->second, b2_it->second, frame);
            sys.AddLink(revolute);

        } else if (type == "prismatic") {
            auto b1_it = result.bodies.find(obj["body_1"].get<std::string>());
            auto b2_it = result.bodies.find(obj["body_2"].get<std::string>());
            if (b1_it == result.bodies.end() || b2_it == result.bodies.end()) {
                result.warnings.push_back("prismatic '" + id + "': body not found");
                continue;
            }
            auto pos_vec = json_get(obj, "pos", std::vector<double>{0, 0, 0});
            chrono::ChVector3d pos(pos_vec[0], pos_vec[1], pos_vec[2]);
            auto prismatic = chrono_types::make_shared<chrono::ChLinkLockPrismatic>();
            chrono::ChFrame<> frame(pos);
            prismatic->Initialize(b1_it->second, b2_it->second, frame);
            sys.AddLink(prismatic);

        } else if (type == "fixed") {
            auto b1_it = result.bodies.find(obj["body_1"].get<std::string>());
            auto b2_it = result.bodies.find(obj["body_2"].get<std::string>());
            if (b1_it == result.bodies.end() || b2_it == result.bodies.end()) {
                result.warnings.push_back("fixed '" + id + "': body not found");
                continue;
            }
            auto pos_vec = json_get(obj, "pos", std::vector<double>{0, 0, 0});
            chrono::ChVector3d pos(pos_vec[0], pos_vec[1], pos_vec[2]);
            auto fixed = chrono_types::make_shared<chrono::ChLinkLockLock>();
            chrono::ChFrame<> frame(pos);
            fixed->Initialize(b1_it->second, b2_it->second, frame);
            sys.AddLink(fixed);

        } else if (type == "motor_shaft_speed") {
            std::string shaft_id = obj["shaft"];
            auto shaft_it = result.shafts.find(shaft_id);
            if (shaft_it == result.shafts.end()) {
                result.warnings.push_back("motor_shaft_speed '" + id + "': shaft not found");
                continue;
            }
            double rpm = json_get(obj, "speed_rpm", 0.0);
            double omega = rpm * 2.0 * M_PI / 60.0;

            auto motor = chrono_types::make_shared<chrono::ChShaftsMotorSpeed>();
            motor->SetName(id);
            motor->SetSpeedFunction(
                chrono_types::make_shared<chrono::ChFunctionConst>(omega));

            // Need a fixed shaft to anchor the motor against.
            // Create one if we don't already have a suitable anchor.
            auto anchor = chrono_types::make_shared<chrono::ChShaft>();
            anchor->SetName(id + "_anchor");
            anchor->SetInertia(0.001);
            anchor->SetFixed(true);
            sys.AddShaft(anchor);

            motor->Initialize(shaft_it->second, anchor);
            sys.Add(motor);
            result.shaft_motors[id] = motor;

        } else if (type == "motor_body_speed") {
            std::string body_id = obj["body"];
            auto body_it = result.bodies.find(body_id);
            if (body_it == result.bodies.end()) {
                result.warnings.push_back("motor_body_speed '" + id + "': body not found");
                continue;
            }
            double rpm = json_get(obj, "speed_rpm", 0.0);
            double omega = rpm * 2.0 * M_PI / 60.0;

            auto motor = chrono_types::make_shared<chrono::ChLinkMotorRotationSpeed>();
            motor->SetName(id);
            motor->SetSpeedFunction(
                chrono_types::make_shared<chrono::ChFunctionConst>(omega));

            chrono::ChFrame<> frame(body_it->second->GetPos());
            motor->Initialize(body_it->second, ground_body, frame);
            sys.AddLink(motor);
            result.body_motors[id] = motor;

        } else {
            result.warnings.push_back("Unknown object type: " + type + " (id: " + id + ")");
        }
    }

    return result;
}

/// Legacy builder: Build a Chrono system from a mechanism definition dict.
/// Kept for backward compatibility — new code should use build_mechanism_from_spec().
inline BuiltMechanism build_mechanism(const json& mechanism_json) {
    BuiltMechanism result;
    result.system = std::make_shared<chrono::ChSystemNSC>();
    result.system->SetGravitationalAcceleration(chrono::ChVector3d(0, 0, 0));
    result.system->SetMaxPenetrationRecoverySpeed(1.0);
    if (auto solver = std::dynamic_pointer_cast<chrono::ChIterativeSolverVI>(result.system->GetSolver())) {
        solver->SetMaxIterations(150);
    }

    auto& sys = *result.system;

    auto ground_body = chrono_types::make_shared<chrono::ChBody>();
    ground_body->SetName("ground");
    ground_body->SetFixed(true);
    ground_body->SetMass(1.0);
    sys.AddBody(ground_body);

    // 1. Create bodies
    for (const auto& part_json : mechanism_json["parts"]) {
        std::string id = part_json["id"];
        bool is_ground = json_get(part_json, "is_ground", false);

        if (is_ground) {
            result.bodies[id] = ground_body;
            continue;
        }

        auto body = chrono_types::make_shared<chrono::ChBody>();
        body->SetName(id);
        double mass = json_get(part_json, "mass_kg", 1.0);
        double inertia = json_get(part_json, "inertia_kg_m2", 0.01);
        body->SetMass(mass);
        body->SetInertiaXX(chrono::ChVector3d(inertia, inertia, inertia));
        body->SetFixed(false);
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

        auto origin_vec = json_get(joint_json, "origin", std::vector<double>{0, 0, 0});
        chrono::ChVector3d origin(origin_vec[0] / 1000.0, origin_vec[1] / 1000.0, origin_vec[2] / 1000.0);

        if (joint_type == "gear_mesh") {
            // Legacy path: uses ChLinkLockGear (known broken in headless mode).
            // New code should use build_mechanism_from_spec() with shaft elements.
            auto gear = chrono_types::make_shared<chrono::ChLinkLockGear>();

            double ratio = json_get(joint_json, "gear_ratio", 1.0);
            if (joint_json.contains("teeth_parent") && joint_json.contains("teeth_child")) {
                int tp = joint_json["teeth_parent"];
                int tc = joint_json["teeth_child"];
                if (tc > 0) ratio = static_cast<double>(tp) / tc;
            }

            chrono::ChFrame<> frame_parent(parent_body->GetPos(), chrono::QuatFromAngleZ(0));
            chrono::ChFrame<> frame_child(child_body->GetPos(), chrono::QuatFromAngleZ(0));

            gear->Initialize(parent_body, child_body, false, frame_parent, frame_child);
            gear->SetTransmissionRatio(ratio);
            gear->SetEpicyclic(json_get(joint_json, "internal", false));

            sys.AddLink(gear);
            result.warnings.push_back(
                "gear_mesh '" + joint_id + "' uses legacy ChLinkLockGear (broken in headless). "
                "Upgrade to simulation_spec path for correct shaft-based gear simulation.");

        } else if (joint_type == "revolute") {
            auto revolute = chrono_types::make_shared<chrono::ChLinkLockRevolute>();
            chrono::ChFrame<> frame(origin);
            revolute->Initialize(parent_body, child_body, frame);
            sys.AddLink(revolute);

        } else if (joint_type == "prismatic") {
            auto prismatic = chrono_types::make_shared<chrono::ChLinkLockPrismatic>();
            chrono::ChFrame<> frame(origin);
            prismatic->Initialize(parent_body, child_body, frame);
            sys.AddLink(prismatic);

        } else if (joint_type == "fixed") {
            auto fixed = chrono_types::make_shared<chrono::ChLinkLockLock>();
            chrono::ChFrame<> frame(origin);
            fixed->Initialize(parent_body, child_body, frame);
            sys.AddLink(fixed);

        } else {
            result.warnings.push_back("Unsupported joint type: " + joint_type + " for joint " + joint_id);
        }
    }

    // 3. Create motors from drives
    for (const auto& drive_json : mechanism_json.value("drives", json::array())) {
        std::string joint_id = drive_json["joint_id"];

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
                        chrono_types::make_shared<chrono::ChFunctionConst>(omega));

                    chrono::ChFrame<> frame(parent_it->second->GetPos());
                    motor->Initialize(parent_it->second, ground_body, frame);
                    sys.AddLink(motor);
                    result.body_motors[joint_id] = motor;
                }
                break;
            }
        }
    }

    return result;
}

} // namespace chrono_daemon
