Build the winning prop. Use geometry.propeller_blade with the winner's diameter_mm, num_blades, chord_root_mm, chord_tip_mm, and num_sections=6. Pitch is derived from the winner's twist_root_deg.

Follow the returned build_hint EXACTLY:

1. For each of the 6 sections: cad.sketch on an offset XZ datum plane at section.plane_offset_mm using the section's geometry_ref
2. cad.loft across all 6 section sketches (creates the blade)
3. cad.polar_pattern with occurrences=num_blades around the prop spin axis to make the multi-bladed prop

Mount the prop body on each of the 4 motor mounts. Either:
  a. cad.linear_pattern with the 4 motor positions, or
  b. clone the prop body into 4 instances and place each via cad.set_placement at the motor positions

Take a final beauty shot of the assembled drone (orbit camera).

Report:
- Final hover time vs. a baseline (a non-optimized 18" 2-blade prop with stock NACA 4412 chord/twist) so we can show the delta.
- How much of the search space the optimization spanned.
