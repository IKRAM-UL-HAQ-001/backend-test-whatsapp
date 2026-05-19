import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def cleanup_expired_statuses():
    from .models import UserStatus

    expired = UserStatus.objects.filter(expires_at__lte=timezone.now(), is_active=True)
    count = expired.count()
    for s in expired:
        try:
            if s.media_file:
                s.media_file.delete(save=False)
            if s.thumbnail:
                s.thumbnail.delete(save=False)
        except Exception as exc:
            logger.warning("Failed to delete media for status %s: %s", s.id, exc)
    expired.update(is_active=False)
    logger.info("Cleaned up %d expired statuses", count)
    return count
