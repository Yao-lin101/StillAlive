from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import date, datetime, timedelta
from apps.characters.models import Character, DailyReportConfig, DailyReport
from apps.characters.tasks import aggregate_status_data, analyze_with_llm
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Generate daily report for a specific character and date'

    def add_arguments(self, parser):
        parser.add_argument(
            'character_uid',
            type=str,
            help='Character UID to generate report for'
        )
        parser.add_argument(
            'date',
            type=str,
            help='Date to generate report for (format: YYYY-MM-DD)'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force regenerate even if report already exists'
        )
        parser.add_argument(
            '--no-ai',
            action='store_true',
            help='Skip AI analysis (for testing purposes)'
        )
        parser.add_argument(
            '--update',
            action='store_true',
            help='Update existing report instead of deleting (use with --force to overwrite)'
        )

    def handle(self, *args, **options):
        character_uid = options['character_uid']
        date_str = options['date']
        force = options['force']
        no_ai = options['no_ai']
        update_mode = options['update']

        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            self.stderr.write(
                self.style.ERROR(
                    f'Invalid date format: {date_str}. Please use YYYY-MM-DD format.'
                )
            )
            return

        now = timezone.now()
        local_now = timezone.localtime(now)
        today = local_now.date()
        
        if target_date == today:
            data_cutoff_time = local_now
            self.stdout.write(
                self.style.NOTICE(
                    f'Generating report for today ({target_date}), data cutoff at {data_cutoff_time.isoformat()}'
                )
            )
        else:
            start_of_day = timezone.make_aware(datetime.combine(target_date, datetime.min.time()))
            data_cutoff_time = start_of_day + timedelta(days=1)
            self.stdout.write(
                self.style.SUCCESS(
                    f'Generating daily report for character {character_uid} on {target_date.isoformat()} (cutoff: {data_cutoff_time.isoformat()})'
                )
            )

        try:
            character = Character.objects.get(uid=character_uid)
        except Character.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(f'Character not found: {character_uid}')
            )
            return

        try:
            config = DailyReportConfig.objects.select_related('character').get(character=character)
        except DailyReportConfig.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(
                    f'Daily report config not found for character: {character.name}. '
                    f'Please enable daily report in character settings first.'
                )
            )
            return

        if not config.is_enabled:
            self.stdout.write(
                self.style.WARNING(
                    f'Warning: Daily report is not enabled for character: {character.name}'
                )
            )
            confirm = input('Continue anyway? (y/n): ')
            if confirm.lower() != 'y':
                self.stdout.write(self.style.WARNING('Aborted by user'))
                return

        existing_report = DailyReport.objects.filter(
            character=character,
            date=target_date
        ).first()

        if existing_report:
            if force:
                if update_mode:
                    self.stdout.write(
                        self.style.WARNING(
                            f'Existing report found. Will update with new data (force update mode)...'
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            f'Existing report found. Deleting and regenerating (force mode)...'
                        )
                    )
                    existing_report.delete()
                    existing_report = None
            else:
                self.stderr.write(
                    self.style.ERROR(
                        f'Report already exists for {character.name} on {target_date.isoformat()}. '
                        f'Use --force to regenerate or --force --update to update.'
                    )
                )
                return

        field_mappings = config.field_mappings or {}

        if not field_mappings:
            self.stdout.write(
                self.style.WARNING(
                    f'Warning: No field mappings configured for character: {character.name}'
                )
            )
            confirm = input('Continue anyway? (y/n): ')
            if confirm.lower() != 'y':
                self.stdout.write(self.style.WARNING('Aborted by user'))
                return

        self.stdout.write(
            self.style.NOTICE(
                f'Aggregating status data for {character.name}...'
            )
        )

        aggregated_data = aggregate_status_data(
            character, 
            field_mappings, 
            target_date,
            end_datetime=data_cutoff_time
        )

        if not aggregated_data:
            self.stderr.write(
                self.style.ERROR(
                    f'No status data found for {character.name} on {target_date.isoformat()}'
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully aggregated {aggregated_data["total_records"]} records'
            )
        )

        new_last_record_time_str = aggregated_data.get('last_record_time')
        new_last_record_time = None
        if new_last_record_time_str:
            try:
                new_last_record_time = timezone.datetime.fromisoformat(new_last_record_time_str)
                if timezone.is_naive(new_last_record_time):
                    new_last_record_time = timezone.make_aware(new_last_record_time)
                self.stdout.write(
                    self.style.NOTICE(
                        f'Latest record time: {new_last_record_time.isoformat()}'
                    )
                )
            except (ValueError, TypeError):
                self.stdout.write(
                    self.style.WARNING(
                        f'Failed to parse last_record_time: {new_last_record_time_str}'
                    )
                )

        if no_ai:
            self.stdout.write(
                self.style.WARNING(
                    f'Skipping AI analysis (--no-ai flag set)'
                )
            )
            analysis_result = {
                'markdown': '## 分析已跳过\n\nAI 分析已跳过（测试模式）',
                'error': 'AI analysis skipped for testing'
            }
        else:
            self.stdout.write(
                self.style.NOTICE(
                    f'Analyzing data with LLM...'
                )
            )
            analysis_result = analyze_with_llm(aggregated_data, character.name)

            if analysis_result.get('error'):
                self.stdout.write(
                    self.style.WARNING(
                        f'LLM analysis warning: {analysis_result["error"]}'
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Successfully analyzed with LLM'
                    )
                )

        if existing_report:
            existing_report.raw_data = aggregated_data
            existing_report.analysis_result = analysis_result
            existing_report.last_record_time = new_last_record_time
            existing_report.data_cutoff_time = data_cutoff_time
            existing_report.save()
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'\nDaily report updated successfully!'
                )
            )
        else:
            DailyReport.objects.create(
                character=character,
                date=target_date,
                is_hidden=False,
                raw_data=aggregated_data,
                analysis_result=analysis_result,
                last_record_time=new_last_record_time,
                data_cutoff_time=data_cutoff_time
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f'\nDaily report generated successfully!'
                )
            )

        self.stdout.write(
            self.style.NOTICE(
                f'  Character: {character.name}'
            )
        )
        self.stdout.write(
            self.style.NOTICE(
                f'  Date: {target_date.isoformat()}'
            )
        )
        self.stdout.write(
            self.style.NOTICE(
                f'  Data cutoff: {data_cutoff_time.isoformat()}'
            )
        )
        self.stdout.write(
            self.style.NOTICE(
                f'  Records processed: {aggregated_data["total_records"]}'
            )
        )
        
        markdown = analysis_result.get('markdown', '')
        if markdown:
            lines = markdown.strip().split('\n')
            if lines:
                first_line = lines[0]
                self.stdout.write(
                    self.style.NOTICE(
                        f'\nTitle: {first_line}'
                    )
                )
