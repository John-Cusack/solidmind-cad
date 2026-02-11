DEFAULT_PROCESS = "print_3d"
SUPPORTED_PROCESSES = ("cnc", "print_3d")

SUPPORTED_SPEC_MAJOR = 1

MATURITY_LEVELS = ("L1", "L2", "L3")

# Deterministic coverage thresholds (MVP defaults).
# These can be tuned later without changing the schema shape.
COVERAGE_THRESHOLDS = {
    "L1": 0.60,
    "L2": 0.80,
    "L3": 0.90,
}

# Canonical hash algorithm label promised by the spec.
HASH_ALGO = "sha256_jcs_rfc8785"

# FreeCAD addon socket defaults.
FREECAD_HOST = "127.0.0.1"
FREECAD_PORT = 9876
