# Part-class failure-mode taxonomy

Each `*.yaml` file in this directory is a small, hand-curated catalog of the
failure modes that an engineer would watch for on a given **part class**. The
inner loop's **Reflect** step (`server/failure_modes.py`) reads them to build a
`ReflectExpectations` *before* running an `analysis.*` check: which failure
modes to check, where the hotspot is expected to land, and a plausible
peak-stress band. A result that lands outside the band — or fails in an unlisted
mode — is a surprise worth learning from.

This is the shared promotion of the format the foam-dart example proved with its
own local `failure_modes.yaml`. The `part_class` field on `design.save_brief` /
`design.add_part` is the key that dispatches into this catalog.

## Format

```yaml
part_classes:
  <part_class_key>:
    description: One-line plain-English description of the part and its job.
    failure_modes_to_check: [stress_concentration, yield, fatigue]   # FailureMode enum values
    expected_hotspot: tooth_root        # free-form label for where peak stress should land
    expected_peak_stress_mpa: [15, 60]  # plausible [low, high] band for peak von Mises
    notes: >
      Optional engineering rationale — the dominant mechanism and the usual fix.
```

`failure_modes_to_check` values must be members of
`server.analysis_models.FailureMode`
(`stress_concentration`, `yield`, `fatigue`, `buckling`, `contact`,
`deflection`, `resonance`, `thermal`, `wear`, `corrosion`). Unknown values are
skipped with a warning so one bad entry can't poison the catalog.

Multiple files are merged; if two files define the same `part_class` key, the
later-loaded one wins (the loader sorts files by name, and caller-supplied
`extra_dirs` override the built-ins). Keep keys in `snake_case` and matching the
`part_class` you set on the brief.
