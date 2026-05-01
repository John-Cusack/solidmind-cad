Now optimize the rotor system for hover time. Use the parametric study infrastructure:

study.create with:
  solver: "bemt_xfoil"
  fixed_params:
    airfoil: "NACA4412"
    Re: 200000
    rpm: 4000
    rho: 1.225
    hover_thrust_N_per_rotor: 14.7
  param_ranges:
    diameter_mm: [400, 460, 500, 540, 580]
    num_blades: [2, 3]
    chord_root_mm: [25, 30, 35, 40]
    chord_tip_mm: [10, 14, 18]
    twist_root_deg: [20, 24, 28]
    twist_tip_deg: [6, 10, 14]
  primary_metric: "efficiency"

study.run, then study.status until done, then study.results top_n=5.

After results: compute hover_time_min for each top-5 variant inline:
  P_total_W      = power_W × 4 / 0.85
  hover_time_min = (300 / P_total_W) × 60

Present a ranked table:
  | Rank | dia_mm | blades | chord_root | chord_tip | twist_root | twist_tip | power_W | FoM  | hover_min |

Pick the winner (highest hover_time_min). Tell me the winner's params and hover time before continuing.
