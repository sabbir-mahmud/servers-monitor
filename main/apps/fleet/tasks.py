import logging

from celery import shared_task

from .services import prune_samples

logger = logging.getLogger(__name__)


@shared_task(name="fleet.prune_metrics", acks_late=True, time_limit=600, soft_time_limit=540)
def prune_metrics():
    deleted = prune_samples()
    logger.info("Pruned %d metric samples past retention", deleted)
    return deleted
