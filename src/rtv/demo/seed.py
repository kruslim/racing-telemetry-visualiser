"""Seed the generated demo session into the store, idempotently."""

from __future__ import annotations

from rtv.demo.generator import DEMO_SESSION_ID, generate_demo_session
from rtv.logging import get_logger
from rtv.store.repository import Repository
from rtv.store.writer import TelemetryWriter

log = get_logger("demo.seed")


def _already_seeded(repo: Repository, session_id: str) -> bool:
    session = repo.get_session(session_id)
    if session is None:
        return False
    # A session row with laps and telemetry files means we are done.
    laps = repo.get_laps(session_id)
    return bool(laps)


def seed_demo_session(
    repo: Repository, writer: TelemetryWriter, *, force: bool = False
) -> str | None:
    """Create the demo session if it isn't already present. Returns its id, or
    ``None`` if it already existed (and ``force`` is False)."""
    if not force and _already_seeded(repo, DEMO_SESSION_ID):
        log.info("Demo session %s already present — skipping seed.", DEMO_SESSION_ID)
        return None

    log.info("Generating demo session %s …", DEMO_SESSION_ID)
    demo = generate_demo_session(session_id=DEMO_SESSION_ID)

    writer.begin_session(demo.session, demo.catalog)
    writer.write_telemetry(DEMO_SESSION_ID, demo.table)
    writer.write_laps(DEMO_SESSION_ID, demo.laps)
    writer.end_session(
        DEMO_SESSION_ID,
        sample_count=demo.sample_count,
        ended_at=demo.session.ended_at or 0.0,
    )
    log.info(
        "Seeded demo session %s: %d laps, %d samples.",
        DEMO_SESSION_ID,
        len(demo.laps),
        demo.sample_count,
    )
    return DEMO_SESSION_ID
