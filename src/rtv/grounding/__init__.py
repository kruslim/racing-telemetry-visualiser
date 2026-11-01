"""Deterministic grounding helpers shared by every coach that checks the figures it says.

:mod:`rtv.grounding.units` reads numbers and their units out of prose and knows which unit
family an engine field is reported in. The Layer-3b prose check and the pitwall validator
both use it, so "6 laps" and "28 psi" are read the same way everywhere.
"""

from __future__ import annotations
