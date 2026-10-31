"""Eval cases cut from a real race journal (task F3).

A recorded pitwall run is already an eval case: the journal holds what the agent was
shown (``run_start`` ``input`` and ``state_full``), every tool result it read
(``tool_call`` ``result_full``) and what it answered (``validate`` ``output_full``).
:mod:`evals.real.cases` cuts those runs out of a journal into a small committed case
set, :mod:`evals.real.offline` re-scores the recorded answers with today's validator
and checks (pytest, no key), and ``evals/run_pitwall.py --real <set>`` re-executes
each input against today's prompt and model (:mod:`evals.real.live`).

Case sets live in ``evals/real/<name>/``; see ``evals/real/spa_run/README.md``.
"""
