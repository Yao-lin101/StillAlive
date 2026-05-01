from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.characters.models import DailyReport, DailyReportConfig
from apps.characters.services.data_service import aggregate_status_data
import logging

class Command(BaseCommand):
    help = '重新聚合数据库中所有 DailyReport 的 raw_data，以适配最新的聚合逻辑'

    def handle(self, *args, **options):
        reports = DailyReport.objects.all().order_by('date')
        
        self.stdout.write(self.style.SUCCESS(f'找到 {reports.count()} 份日报数据准备重新聚合...'))
        
        success_count = 0
        failed_count = 0
        skipped_count = 0
        
        for report in reports:
            try:
                character = report.character
                target_date = report.date
                
                config = DailyReportConfig.objects.filter(character=character).first()
                if not config or not config.field_mappings:
                    self.stdout.write(self.style.WARNING(f'跳过: ID {report.id} - 角色 {character.name} 缺少数据映射配置'))
                    skipped_count += 1
                    continue
                    
                field_mappings = config.field_mappings
                end_datetime = report.data_cutoff_time
                
                self.stdout.write(f'正在处理 ID {report.id} (角色: {character.name}, 日期: {target_date})...')
                
                aggregated_data = aggregate_status_data(
                    character,
                    field_mappings,
                    target_date,
                    end_datetime=end_datetime
                )
                
                if aggregated_data:
                    report.raw_data = aggregated_data
                    report.save(update_fields=['raw_data'])
                    success_count += 1
                else:
                    self.stdout.write(self.style.WARNING(f'跳过: ID {report.id} - 无法生成聚合数据（可能无原始状态记录）'))
                    skipped_count += 1
                    
            except Exception as e:
                failed_count += 1
                self.stdout.write(self.style.ERROR(f'处理失败 ID {report.id}: {str(e)}'))
                
        self.stdout.write(self.style.SUCCESS(f'处理完毕！成功更新: {success_count}, 失败: {failed_count}, 跳过: {skipped_count}'))
