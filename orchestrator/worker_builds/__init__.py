"""Reusable, in-process CAD worker builds for the orchestrator loop.

Each builder in this package takes a ``sub_spec`` dict plus an ``output_dir``
and produces a real STEP file, STL, screenshot, and ``metadata.json`` by
driving the running FreeCAD addon over TCP. The builders are the concrete
answer to the ``test_orchestrator_e2e.py`` question *"what does a real
worker build look like?"* — they replace the fake STEP file the trust-mode
test writes with geometry produced by actual ``cad.*`` commands.

Design notes:

* Builders are synchronous wrappers around the helpers in
  ``orchestrator.worker_entry`` (``_build_envelope``, ``_build_gear``,
  ``_build_ring_gear``, ``_build_carrier``). Those helpers were written for
  the Docker worker path; this package exposes them as a plain Python
  function call the test harness can reach without the A2A server,
  ``claude --print`` subprocess, or Docker container.

* The ``_build_*`` helpers accept a ``task`` argument used only for
  progress logging. The common module ships a ``TaskStub`` class that
  mimics the shape (``task.progress.append(...)``) so we don't need the
  real ``A2ATask`` type from ``orchestrator.a2a_server``.

* Every builder requires a running FreeCAD addon on ``127.0.0.1:9876`` (or
  the ``FREECAD_HOST``/``FREECAD_PORT`` environment variables). Tests gate
  on ``common.freecad_ready()`` which returns False if the socket doesn't
  answer, so CI runs without FreeCAD installed still pass.

* Metadata written to ``output_dir/metadata.json`` follows the schema
  ``orchestrator/measure.py`` + ``orchestrator/validator.py`` expect:
  ``{subsystem, claimed_mass_kg, claimed_bounding_box_mm, interface_actuals,
  deviations, notes}``. The ``interface_actuals`` values are the *claimed*
  measurements the verify-mode path then cross-checks by re-importing the
  STEP file via ``orchestrator.measure.verify_worker_measurements``.

Usage in a test::

    from orchestrator.worker_builds import common, sun_gear

    if not common.freecad_ready():
        self.skipTest("FreeCAD addon not available on :9876")

    step_path = sun_gear.build_sun_gear(sub_spec, output_dir)
    assert step_path.exists() and step_path.stat().st_size > 0
"""
from __future__ import annotations

__all__ = ["common"]
