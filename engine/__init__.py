"""RecoverAI engine package.

Agents: ingest (1), draft (2), orchestrator (3), plus the guardrails, the onboarding
back end and the inbound webhook. Run the demo with `python3 run_demo.py`, the tests
with `python3 -m unittest discover -s tests -t .`.
"""
from .app import RecoverAI  # noqa: F401

__all__ = ["RecoverAI"]
