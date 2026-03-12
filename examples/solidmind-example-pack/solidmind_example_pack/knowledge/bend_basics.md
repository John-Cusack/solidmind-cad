---
domain: sheetmetal
topic: bend_allowance
confidence: textbook
source: "Machinery's Handbook, 31st ed"
---

## Bend allowance formula

Bend allowance (BA) is the arc length of the neutral axis during bending:

    BA = (pi / 180) * bend_angle * (R + K * T)

Where:
- R = inside bend radius
- T = material thickness
- K = K-factor (ratio of neutral axis offset to thickness)

## Common K-factors

| Material | K-factor |
|----------|----------|
| Soft copper, brass | 0.35 |
| Cold-rolled steel | 0.33 |
| Aluminum 5052 | 0.33 |
| Stainless 304 | 0.30 |
| Spring steel | 0.25 |

## Minimum bend radius

Rule of thumb: minimum inside bend radius = 1x material thickness for ductile metals, 2-3x for harder alloys. Below this, cracking risk increases significantly.
