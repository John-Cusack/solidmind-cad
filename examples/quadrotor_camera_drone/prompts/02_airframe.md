Approved. Phase 2 — Layout + Build the airframe in FreeCAD.

1. Add interfaces via design.add_interface — bolt patterns connecting arms to the frame, motor mounts to arm tips, payload mount, battery mount.

2. Update the brief with layout positions: arms emanate diagonally from the frame center at ±247.5 mm (for the X-pattern 700 mm wheelbase), payload below frame, battery on top.

3. Show me the layout summary. Wait for my approval.

After I approve, build the airframe in FreeCAD:

- cad.new_document("CameraDrone")
- Frame center body — rectangular pad ~200×200×30 mm via cad.sketch on XY + cad.pad
- One arm — rectangular pad extending out from the frame; use cad.polar_pattern with occurrences=4 around Z to make all 4 arms
- Motor mounts — small cylinders at arm tips (radius 25 mm, height 20 mm). Place via cad.set_placement at the four ±247.5 mm corner positions
- Payload block — 100×80×60 mm box centred BELOW the frame
- Battery pack — 100×80×40 mm box centred ABOVE the frame

Take a verification screenshot after each major step. Mark each part "built" via design.update_part as you go. Stop when the airframe is complete (NO props yet — those are the next phase after we optimize them).
