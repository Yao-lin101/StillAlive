from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.characters.models import Character, DailyReport
from apps.characters.services.important_event_service import extract_important_events_for_report


class Command(BaseCommand):
    help = 'Extract important event memories from daily reports'

    def add_arguments(self, parser):
        parser.add_argument(
            '--character-uid',
            type=str,
            help='Only process reports for this character UID',
        )
        parser.add_argument(
            '--date',
            type=str,
            help='Only process one date (YYYY-MM-DD). Defaults to yesterday.',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=1,
            help='Process the latest N completed days, ending at --date or yesterday.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Re-extract even if source data hash has not changed.',
        )

    def handle(self, *args, **options):
        character_uid = options.get('character_uid')
        date_str = options.get('date')
        days = max(1, options.get('days') or 1)
        force = options.get('force')

        if date_str:
            try:
                end_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                self.stderr.write(self.style.ERROR('Invalid --date. Use YYYY-MM-DD.'))
                return
        else:
            end_date = timezone.localdate() - timedelta(days=1)

        start_date = end_date - timedelta(days=days - 1)
        reports = DailyReport.objects.filter(date__gte=start_date, date__lte=end_date).select_related('character')

        if character_uid:
            try:
                character = Character.objects.get(uid=character_uid)
            except Character.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'Character not found: {character_uid}'))
                return
            reports = reports.filter(character=character)

        total = reports.count()
        self.stdout.write(self.style.NOTICE(f'Processing {total} daily reports from {start_date} to {end_date}'))

        created = 0
        updated = 0
        skipped = 0
        failed = 0

        for report in reports.order_by('date', 'character_id'):
            try:
                result = extract_important_events_for_report(report, force=force)
                if result.get('skipped'):
                    skipped += 1
                    self.stdout.write(
                        self.style.WARNING(f'Skipped {report.character.name} {report.date}: {result.get("reason")}')
                    )
                    continue

                created += result.get('created', 0)
                updated += result.get('updated', 0)
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Processed {report.character.name} {report.date}: '
                        f'created={result.get("created", 0)}, updated={result.get("updated", 0)}'
                    )
                )
            except Exception as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f'Failed {report.character.name} {report.date}: {exc}'))

        self.stdout.write(
            self.style.SUCCESS(
                f'Done. reports={total}, created={created}, updated={updated}, skipped={skipped}, failed={failed}'
            )
        )
