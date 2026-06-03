from django.core.management.base import BaseCommand
from django.conf import settings
from apps.characters.models import Character, ImportantEvent
from apps.characters.services.important_event_service import sync_events_to_milvus
from apps.characters.services.vector_backends import get_vector_backend


class Command(BaseCommand):
    help = 'Rebuild the vector memory index for important events from the Postgres database (backend-agnostic: Milvus or pgvector)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--character-uid',
            type=str,
            help='Only rebuild memory for this character UID',
        )
        parser.add_argument(
            '--clean',
            action='store_true',
            help='Wipe the existing vectors before rebuilding (Milvus: drop collection; pgvector: clear embedding column)',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=5,
            help='Batch size for syncing events (default is 5)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force sync all events, even if they are already marked as synced',
        )

    def handle(self, *args, **options):
        character_uid = options.get('character_uid')
        clean = options.get('clean')
        batch_size = max(1, options.get('batch_size') or 5)
        force = options.get('force')

        if not getattr(settings, 'IMPORTANT_EVENT_VECTOR_ENABLED', True):
            self.stdout.write(self.style.WARNING("WARNING: IMPORTANT_EVENT_VECTOR_ENABLED is False in settings!"))

        # 1. Resolve the active vector backend
        backend = get_vector_backend()
        self.stdout.write(self.style.NOTICE(f"Active vector backend: {backend.name}"))
        if backend.name == 'none':
            self.stderr.write(self.style.ERROR(
                "Error: no usable vector backend (VECTOR_BACKEND resolved to 'none'). "
                "Install pgvector or configure Milvus."
            ))
            return
        if not backend.is_available():
            self.stderr.write(self.style.ERROR(
                f"Error: vector backend '{backend.name}' is not available "
                f"(check connection / migration / dependency)."
            ))
            return

        # 2. Optionally wipe existing vectors
        if clean:
            self.stdout.write(self.style.WARNING(f"Cleaning existing vectors in backend '{backend.name}'..."))
            if backend.reset():
                self.stdout.write(self.style.SUCCESS("Vectors cleaned."))
            else:
                self.stderr.write(self.style.ERROR("Failed to clean existing vectors."))
                return

        # 3. Retrieve events from Postgres
        events = ImportantEvent.objects.filter(is_active=True)
        if character_uid:
            try:
                character = Character.objects.get(uid=character_uid)
                events = events.filter(character=character)
                self.stdout.write(self.style.NOTICE(f"Filtering events for character: {character.name} ({character_uid})"))
            except Character.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"Error: Character not found with UID: {character_uid}"))
                return

        # If clean or force, reset synced flags so events get fully re-synced.
        if clean or force:
            self.stdout.write(self.style.NOTICE("Resetting synced flags in database..."))
            count = events.update(milvus_synced=False, milvus_synced_at=None)
            self.stdout.write(self.style.NOTICE(f"Reset synced flag for {count} events."))
        else:
            # Otherwise, only sync events that are currently not synced
            events = events.filter(milvus_synced=False)

        total_count = events.count()
        if total_count == 0:
            self.stdout.write(self.style.SUCCESS("No active unsynced events found. Rebuild not needed."))
            return

        self.stdout.write(self.style.NOTICE(f"Starting rebuild sync for {total_count} events in batches of {batch_size}..."))

        event_list = list(events.order_by('date'))

        success_count = 0
        for i in range(0, total_count, batch_size):
            batch = event_list[i:i + batch_size]
            self.stdout.write(self.style.NOTICE(f"Processing batch {i//batch_size + 1} ({len(batch)} events)..."))
            try:
                synced = sync_events_to_milvus(batch)
                success_count += synced
                self.stdout.write(self.style.SUCCESS(f"  Successfully synced {synced}/{len(batch)} events."))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  Failed syncing batch starting at index {i}: {e}"))

        self.stdout.write(self.style.SUCCESS(
            f"Done! Rebuilt vector memory index via '{backend.name}'. Synced {success_count}/{total_count} events."
        ))
