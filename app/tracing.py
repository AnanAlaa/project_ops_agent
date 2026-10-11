"""Tracing shim: uses the Agents SDK trace() when the SDK is installed, otherwise does nothing.

Teammate (task T6): wrap each major workflow with `workflow_trace("WF-01 project update")` and show the trace in the demo.
"""
from contextlib import nullcontext


def workflow_trace(name: str):
    try:
        from agents import trace
        return trace(name)
    except Exception:           # SDK missing or tracing unavailable: never break the workflow
        return nullcontext()
