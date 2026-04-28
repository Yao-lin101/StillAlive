from django.core.management.base import BaseCommand
from apps.characters.models import Character, DailyReportConfig, DailyReport
from apps.characters.tasks import update_system_persona

class Command(BaseCommand):
    help = 'Manually trigger the system persona update for a character'

    def add_arguments(self, parser):
        parser.add_argument(
            'character_uid',
            type=str,
            help='Character UID to update persona for'
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Clear existing persona and force a 7-day retrospective rebuild'
        )

    def handle(self, *args, **options):
        character_uid = options['character_uid']

        try:
            character = Character.objects.get(uid=character_uid)
        except Character.DoesNotExist:
            self.stderr.write(self.style.ERROR(f'Character not found: {character_uid}'))
            return

        try:
            config = DailyReportConfig.objects.get(character=character)
        except DailyReportConfig.DoesNotExist:
            self.stderr.write(self.style.ERROR('Daily report config not found for this character.'))
            return

        if options['reset']:
            config.system_inferred_persona = ''
            config.save(update_fields=['system_inferred_persona'])
            self.stdout.write(self.style.WARNING(f'Cleared existing system persona for {character.name}. Will rebuild from 7-day history.'))

        # Fetch the most recent report to pass to the function
        latest_report = DailyReport.objects.filter(character=character, is_hidden=False).order_by('-date').first()
        report_text = latest_report.analysis_result.get('markdown', '') if latest_report else ''

        self.stdout.write(self.style.NOTICE(f'Triggering system persona update for {character.name}...'))
        
        # Call the existing logic
        update_system_persona(config, report_text)
        
        # Refresh from db to get the newly generated persona
        config.refresh_from_db()
        
        self.stdout.write(self.style.SUCCESS('\n================================='))
        self.stdout.write(self.style.SUCCESS('Generation Completed!'))
        self.stdout.write(self.style.SUCCESS('=================================\n'))
        
        if config.system_inferred_persona:
            self.stdout.write(self.style.NOTICE('【The New System Inferred Persona】:'))
            self.stdout.write(config.system_inferred_persona)
        else:
            report_count = DailyReport.objects.filter(character=character, is_hidden=False).count()
            if report_count < 3:
                self.stdout.write(self.style.WARNING(f'Skipped: Not enough data ({report_count} days). The system requires at least 3 days of data to establish a reliable baseline profile.'))
            else:
                self.stdout.write(self.style.ERROR('Failed to generate persona or result is empty.'))
