"""The Engine Integration Contract Test Kit.

Run it against any engine, in any language, from anywhere::

    python3 -m tck --host 127.0.0.1 --port 9880

It is the **support boundary**: engine bug reports start with "attach your TCK
output".  Passing it means an engine speaks the contract
(``docs/engine-contract.md``); failing it says exactly which tier and which
claim broke.

Six tiers, each independently reportable (contract §3.6):

1. **Protocol** — handshake, framing, error taxonomy, capability honesty.
2. **Package** — ingests the golden sim package.
3. **Results** — ``simulate`` output validates against the result schema.
4. **Sessions / teleop** — only checked when the engine advertises them.
5. **Physics sanity** — golden scenarios with analytic answers.
6. **Latency** — informational RTT distribution.

The kit imports nothing from ``server``: an engine repository can vendor this
directory and run it with a stock Python.
"""

from tck.report import Outcome, TckReport, TierResult

__all__ = ["Outcome", "TckReport", "TierResult", "run_tck"]


def run_tck(*args, **kwargs):  # pragma: no cover - thin re-export
    from tck.runner import run_tck as _run_tck

    return _run_tck(*args, **kwargs)
