"""Experiment scaffold — state machine, budget guards, and Mayor backends.

See docs/EXPERIMENT-PLAN.md for the architectural rationale. The heartbeat
tick is a thin shell entry point that calls into this package; all
transition logic, budget evaluation, and IO live here so they can be
unit-tested without spawning real subprocesses.
"""
