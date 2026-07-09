"""Coaching evaluation harness.

The distinctive piece is :mod:`evals.checks`: because the deterministic findings are
ground truth, we can cross-check every figure the LLM coach produced against them and
catch hallucinations automatically — no LLM judge required for factuality. An optional
LLM judge (:mod:`evals.judges`) scores the softer qualities (clarity, actionability).
"""
