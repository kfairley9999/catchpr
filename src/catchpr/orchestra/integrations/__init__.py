"""Wiring for the real agent teams.

These modules import the team packages lazily, so catchpr itself does not
depend on them. Put the team package on ``PYTHONPATH`` (or install it) in the
environment that runs the orchestrator.
"""
