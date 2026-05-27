from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from apps.characters.models import Character, ImportantEvent
from apps.characters.services.important_event_service import (
    sync_events_to_milvus,
    _get_milvus_collection,
)

class Command(BaseCommand):
    help = 'Rebuild the Milvus memory index for important events from Postgres database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--character-uid',
            type=str,
            help='Only rebuild memory for this character UID',
        )
        parser.add_argument(
            '--clean',
            action='store_true',
            help='Drop the existing Milvus collection and recreate it from scratch',
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
            help='Force sync all events, even if they are already marked as milvus_synced=True',
        )

    def handle(self, *args, **options):
        character_uid = options.get('character_uid')
        clean = options.get('clean')
        batch_size = max(1, options.get('batch_size') or 5)
        force = options.get('force')

        if not getattr(settings, 'IMPORTANT_EVENT_VECTOR_ENABLED', True):
            self.stdout.write(self.style.WARNING("WARNING: IMPORTANT_EVENT_VECTOR_ENABLED is False in settings!"))

        # 1. Initialize Milvus collection connection
        self.stdout.write(self.style.NOTICE("Connecting to Milvus..."))
        collection = _get_milvus_collection()
        if collection is None:
            self.stderr.write(self.style.ERROR("Error: Failed to connect to Milvus or pymilvus is not installed."))
            return

        collection_name = collection.name
        alias = 'important_event_memory'

        # 2. Clean Milvus collection if requested
        if clean:
            self.stdout.write(self.style.WARNING(f"Dropping collection '{collection_name}' in Milvus..."))
            try:
                from pymilvus import utility
                if utility.has_collection(collection_name, using=alias):
                    utility.drop_collection(collection_name, using=alias)
                    self.stdout.write(self.style.SUCCESS(f"Successfully dropped collection '{collection_name}'."))
                else:
                    self.stdout.write(self.style.NOTICE(f"Collection '{collection_name}' does not exist, nothing to drop."))
                
                # Re-fetch/re-create the collection schema and indexes
                self.stdout.write(self.style.NOTICE(f"Re-creating and loading collection '{collection_name}'..."))
                collection = _get_milvus_collection()
                if collection is None:
                    self.stderr.write(self.style.ERROR("Error: Failed to re-create/load Milvus collection after drop."))
                    return
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Failed to drop/recreate Milvus collection: {e}"))
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

        # If clean was specified, or if force was specified, we should reset milvus_synced to False in SQL
        # so that it is clear they are being fully re-synced.
        if clean or force:
            self.stdout.write(self.style.NOTICE("Resetting milvus_synced flags in database..."))
            count = events.update(milvus_synced=False, milvus_synced_at=None)
            self.stdout.write(self.style.NOTICE(f"Reset milvus_synced=False for {count} events."))
        else:
            # Otherwise, only sync events that are currently not synced
            events = events.filter(milvus_synced=False)

        total_count = events.count()
        if total_count == 0:
            self.stdout.write(self.style.SUCCESS("No active unsynced events found. Rebuild not needed."))
            return

        self.stdout.write(self.style.NOTICE(f"Starting rebuild sync for {total_count} events in batches of {batch_size}..."))

        # Convert to list/iterator for batching
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
            f"Done! Rebuilt Milvus memory index. Synced {success_count}/{total_count} events."
        ))
