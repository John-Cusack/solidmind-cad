Now verify the design actually flies. Drive the full simulation pipeline:

1. Build the mechanism: motion.define_mechanism with 4 continuous joints named rotor_FL_joint, rotor_FR_joint, rotor_RR_joint, rotor_RL_joint. Axis (0, 0, 1) at each motor mount, prop bodies as the rotating links.

2. Export the sim package + PX4 airframe params via cad.export_sim_package:
   - mechanism_id = <id from step 1>
   - emit_sdf = true
   - drone_config = {
       "rotors": [
         {"index": 0, "joint": "rotor_FL_joint", "direction": "ccw", "position_m": (0.2475, 0.2475, 0)},
         {"index": 1, "joint": "rotor_FR_joint", "direction": "cw",  "position_m": (0.2475, -0.2475, 0)},
         {"index": 2, "joint": "rotor_RR_joint", "direction": "ccw", "position_m": (-0.2475, -0.2475, 0)},
         {"index": 3, "joint": "rotor_RL_joint", "direction": "cw",  "position_m": (-0.2475, 0.2475, 0)},
       ],
       "sensors": True,
       "px4": True,
       "register_airframe": True,
     }

   Report the airframe_id (a SYS_AUTOSTART number in 50000-50999), airframe_path, and computed hover_throttle from the result.

3. Rebuild PX4 with the new airframe. Run via shell:
     cd ~/repos/PX4-Autopilot && make px4_sitl <airframe_name>
   This is just to register the new airframe in PX4's catalog (~30 s incremental build).

4. Launch PX4 SITL with the custom airframe in the background:
     cd ~/repos/PX4-Autopilot && make px4_sitl <airframe_name> &
   Wait until "Startup script returned successfully" appears in the PX4 log.

5. Connect the bridge's MavlinkController and fly:
   - Stream HEARTBEAT at 3 Hz from sys_id=255
   - Set permissive params: COM_RC_IN_MODE=4, NAV_RCL_ACT=0, NAV_DLL_ACT=0
   - Wait for sensors healthy in SYS_STATUS (gyro+accel+mag+baro bits set)
   - controller.arm() — uses force-arm magic 21196 by default
   - controller.takeoff_via_mode() — DO_SET_MODE → AUTO_TAKEOFF (main=4 sub=2)
   - Hold for 30 s while the drone hovers in Gazebo at MIS_TAKEOFF_ALT
   - controller.land_via_mode() — DO_SET_MODE → AUTO_LAND (main=4 sub=6)
   - Wait for landed/disarmed

Report the final hover time alongside the BEMT prediction from the optimization phase. The visible flight in Gazebo is the proof that the optimization landed somewhere flyable.
