import logging

logger = logging.getLogger(__name__)


def safe_delay(task, *args, **kwargs):
    """Enqueue; return AsyncResult or None. Never raises: a broker outage must not fail a
    heartbeat or a page — the work is logged as dropped instead."""
    try:
        return task.delay(*args, **kwargs)
    except Exception:  # deliberately broad: every enqueue failure has the same answer
        logger.exception(
            "Could not enqueue %s; the work has been dropped.", getattr(task, "name", task)
        )
        return None
