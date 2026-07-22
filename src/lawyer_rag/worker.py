from __future__ import annotations

import signal
import time

import structlog

from lawyer_rag.config import get_settings
from lawyer_rag.db import create_schema, session_scope
from lawyer_rag.ingestion import claim_job, process_job
from lawyer_rag.logging_config import configure_logging


logger = structlog.get_logger()
running = True


def _stop(_signum: int, _frame: object) -> None:
    global running
    running = False


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    create_schema()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("worker_started")

    while running:
        with session_scope() as session:
            job = claim_job(session)
            job_id = job.id if job else None
        if not job_id:
            time.sleep(settings.worker_poll_seconds)
            continue
        try:
            process_job(job_id, settings)
        except Exception:
            continue

    logger.info("worker_stopped")


if __name__ == "__main__":
    main()
