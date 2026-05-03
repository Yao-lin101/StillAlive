from django.apps import AppConfig
from django.conf import settings
import logging
import threading


logger = logging.getLogger(__name__)


class CharactersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.characters"
    verbose_name = "角色管理"

    def ready(self):
        import apps.characters.signals  # noqa: F401
        if getattr(settings, 'IMPORTANT_EVENT_INFRA_CHECK_ON_STARTUP', False):
            from apps.characters.services.important_event_service import check_important_event_infra

            thread = threading.Thread(
                target=check_important_event_infra,
                name='important-event-infra-check',
                daemon=True,
            )
            thread.start()
            logger.info("Important event memory infrastructure check started in background")
