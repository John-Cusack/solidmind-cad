# Engine Integration Contract Test Kit

Proof that an engine speaks the contract. Run it against anything that listens
on a TCP port and answers NDJSON — any language, any physics, any machine.

```bash
python3 -m tck --host 127.0.0.1 --port 9880
```

Exit code `0` means conformant, `1` means it isn't, `2` means the engine
couldn't be reached. `--json` emits the same report as machine-readable data.

**This is the support boundary.** A bug report about an engine starts with its
TCK output; that tells both sides which tier and which claim broke before
anyone reads code.

## What it checks

| Tier | Checks | Skips when |
|---|---|---|
| 1. protocol | `hello` shape, `request_id` echo/omission, the four required verbs, error taxonomy, capability honesty in both directions | never — this tier is the floor |
| 2. package | ingests the golden sim package; a missing one returns `PACKAGE_INVALID` | `package` isn't in `formats` |
| 3. results | `simulate` output validates against `schemas/sim_result.schema.json` | schema or `jsonschema` unavailable |
| 4. sessions/teleop | full session and teleop lifecycles, including `SESSION_NOT_FOUND` after stop | the mode isn't advertised |
| 5. physics sanity | gear ratio (20:40 → half speed, reversed), pendulum period `2π√(L/g)`, free fall settling at `√(2h/g)` | `runtime_mode: "stub"`, `mechanism` not in `formats` (these scenarios are in-band mechanisms), or the engine reports no matching state |
| 6. latency | RTT distribution, informational | never fails a run |

**Capability honesty** is the check most engines get wrong first: every verb
implied by an advertised capability must work, and every verb *not* advertised
must answer `UNSUPPORTED_COMMAND`. Advertising `screenshot` and then refusing
`screenshot` is a failure; so is quietly implementing a verb you never claimed.

## Partial engines are first-class

An engine that implements only `hello`, `ping`, `simulate` and `shutdown` —
no sessions, no teleop, no package ingest — passes with skips, not failures.
The contract is a floor plus opt-ins, and the report shows exactly which
opt-ins you took.

## Running it against your own engine

```bash
# your engine on some port
python3 -m my_engine.bridge --port 9999

# in another shell, from a checkout of solidmind-cad
python3 -m tck --port 9999
python3 -m tck --port 9999 --tier protocol          # just the floor
python3 -m tck --port 9999 --package-dir ./my_pkg   # your own package
python3 -m tck --port 9999 --json > conformance.json
```

The kit imports nothing from `server`, so you can vendor this directory into
your engine repository and run it with a stock Python (plus `jsonschema` for
tier 3).

## The reference engine

`reference_engine/` in this repository is a complete worked example: it passes
every tier, needs no install, and is small enough to read in one sitting.
Start there — clone it, keep the protocol, swap the physics.

```bash
python3 -m reference_engine.bridge_server --port 9880
python3 -m tck --port 9880
```
