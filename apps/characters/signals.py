import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from apps.characters.models import ImportantEvent
from apps.characters.services.important_event_service import delete_event_from_milvus


logger = logging.getLogger(__name__)


@receiver(post_delete, sender=ImportantEvent)
def cleanup_important_event_vector(sender, instance, **kwargs):
    if instance.milvus_synced:
        deleted = delete_event_from_milvus(instance.id)
        if not deleted:
            logger.warning("Failed to cleanup Milvus vector for deleted important event %s", instance.id)
