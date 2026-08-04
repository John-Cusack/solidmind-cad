"""The reference engine — core's own implementation of the contract.

Every other engine lives in its own repository; this one stays in core
forever, for three reasons:

* **CI has an engine.** Core's tests run green with zero engines installed
  because this one needs nothing but Python.
* **The contract has a worked example.** An engine author clones this, swaps
  the physics, and keeps the protocol (``docs/engine-contract.md``).
* **The TCK has a known-good subject.** A TCK failure against the reference
  engine is a bug in the TCK, not in someone's engine.

It is deliberately *not* a vendor's stub wearing core's hat: the Gazebo bridge
keeps its own stub mode for Gazebo's purposes, and this engine keeps a name
that says what it is.
"""
