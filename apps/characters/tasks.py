from celery import shared_task
from django.utils import timezone
from datetime import timedelta, datetime
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.conf import settings
from .models import WillConfig, CharacterStatus, DailyReportConfig, DailyReport
import logging

from .services.data_service import aggregate_status_data
from .services.llm_service import analyze_all_modules_sequential
from .services.persona_service import update_system_persona
from .services.important_event_service import (
    extract_important_events_for_report,
    format_events_for_prompt,
    retrieve_important_events,
)

logger = logging.getLogger(__name__)

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=300,  # 5分钟后重试
    autoretry_for=(Exception,),
    retry_backoff=True,  # 使用指数退避算法
)
def send_will_email(self, will_config_id):
    """
    发送遗嘱邮件的异步任务
    """
    try:
        will_config = WillConfig.objects.select_related('character').get(id=will_config_id)
        
        # 获取最后更新时间
        last_status = CharacterStatus.objects.filter(
            character=will_config.character
        ).order_by('-timestamp').first()
        
        last_updated = last_status.timestamp if last_status else timezone.now()
        
        # 计算自上次更新以来的时间
        now = timezone.now()
        time_since_last_update = now - last_updated
        
        # 格式化时间差为人类可读的格式
        days = time_since_last_update.days
        hours, remainder = divmod(time_since_last_update.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        
        if days > 0:
            time_diff_str = f"{days}天{hours}小时{minutes}分钟"
        elif hours > 0:
            time_diff_str = f"{hours}小时{minutes}分钟"
        else:
            time_diff_str = f"{minutes}分钟"
        
        # 构建角色状态展示链接
        display_url = f"{settings.CHARACTER_DISPLAY_BASE_URL}/d/{will_config.character.display_code}"
        
        # 渲染邮件模板
        html_content = render_to_string('emails/will_notification.html', {
            'character_name': will_config.character.name,
            'content': will_config.content,
            'last_updated': last_updated.strftime('%Y-%m-%d %H:%M:%S'),
            'time_since_last_update': time_diff_str,
            'display_url': display_url
        })

        # 创建邮件
        email = EmailMessage(
            subject=f"紧急通知：{will_config.character.name} 已超过 {will_config.timeout_hours} 小时未更新状态",
            body=html_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[will_config.target_email],
            cc=will_config.cc_emails
        )
        email.content_subtype = "html"
        
        # 发送邮件
        email.send()
        
        logger.info(f"Will email sent successfully for character {will_config.character.name}")
        return True
    except Exception as e:
        logger.error(f"Failed to send will email: {str(e)}")
        raise self.retry(exc=e)



@shared_task
def check_wills():
    """
    定时检查是否需要发送遗嘱
    """
    now = timezone.now()
    logger.info("Starting will check task")
    
    # 获取所有启用了遗嘱功能的配置
    active_wills = WillConfig.objects.filter(
        is_enabled=True
    ).select_related('character')

    logger.info(f"Found {active_wills.count()} active wills")

    for will in active_wills:
        try:
            # 获取角色最后的状态更新时间
            last_status = CharacterStatus.objects.filter(
                character=will.character
            ).order_by('-timestamp').first()

            if not last_status:
                logger.info(f"No status found for character {will.character.name}")
                continue

            # 计算是否超过设定的超时时间
            timeout = timedelta(hours=will.timeout_hours)
            time_since_last_update = now - last_status.timestamp
            
            # 只保留关键日志，移除详细的调试信息
            if time_since_last_update > timeout:
                logger.info(
                    f"Timeout detected for character {will.character.name} "
                    f"(uid: {will.character.uid}). Last status update was {time_since_last_update} ago"
                )
                
                # 禁用遗嘱配置
                will.is_enabled = False
                will.save(update_fields=['is_enabled'])
                
                # 发送邮件通知
                send_will_email.delay(will.id)
                logger.info(f"Will config disabled for character {will.character.name}")
            
        except Exception as e:
            logger.error(f"Error processing will for character {will.character.name}: {str(e)}")
            continue

    logger.info("Will check task completed")



@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=600,
    autoretry_for=(Exception,),
    retry_backoff=True,
    # 强制超时保护：防止任务死锁挂起
    soft_time_limit=1500,  # 25分钟，抛出 SoftTimeLimitExceeded 异常
    time_limit=1800,       # 30分钟，强制杀进程回收资源
)
def generate_daily_reports(self):
    """
    定时生成日报分析（每小时执行）
    
    每小时执行，分析当天截至当前时间的数据
    - 如果日报不存在：创建新日报
    - 如果日报已存在但有新数据：更新日报
    - 如果日报已存在且无新数据：跳过 LLM 分析
    """
    now = timezone.now()
    local_now = timezone.localtime(now)
    today = local_now.date()
    yesterday = today - timedelta(days=1)
    
    # 确定要处理的日期
    if local_now.hour == 0:
        # 0点~1点（如0:05）：只收尾昨天的数据。此时今天才刚开始几分钟，数据太少会浪费大模型API
        target_dates = [yesterday]
    else:
        # 1点之后：正常处理今天的数据
        target_dates = [today]
        # 兜底：在1点~2点再检查一次昨天，防止0点的任务因服务器宕机等原因未执行
        if local_now.hour == 1:
            target_dates.insert(0, yesterday)
    
    logger.info(f"Starting daily report generation at {local_now.isoformat()} (local time)")
    
    active_configs = DailyReportConfig.objects.filter(
        is_enabled=True
    ).select_related('character')
    
    logger.info(f"Found {active_configs.count()} characters with daily report enabled")
    
    success_count = 0
    failed_count = 0
    skipped_count = 0
    updated_count = 0
    new_count = 0
    
    for config in active_configs:
        for target_date in target_dates:
            try:
                character = config.character
                logger.info(f"Processing report for character: {character.name} (uid: {character.uid}) on {target_date}")
                
                field_mappings = config.field_mappings or {}
                
                if not field_mappings:
                    logger.warning(f"No field mappings configured for {character.name}, skipping report")
                    skipped_count += 1
                    continue
                
                # 如果是昨天，数据截止到今天的 00:00:00
                if target_date == today:
                    current_cutoff_time = local_now
                else:
                    current_cutoff_time = timezone.make_aware(datetime.combine(today, datetime.min.time()))
                
                aggregated_data = aggregate_status_data(
                    character, 
                    field_mappings, 
                    target_date,
                    end_datetime=current_cutoff_time
                )
                
                if not aggregated_data:
                    logger.info(f"No status data found for {character.name} on {target_date}")
                    skipped_count += 1
                    continue
                
                new_last_record_time_str = aggregated_data.get('last_record_time')
                new_last_record_time = None
                if new_last_record_time_str:
                    try:
                        new_last_record_time = timezone.datetime.fromisoformat(new_last_record_time_str)
                        if timezone.is_naive(new_last_record_time):
                            new_last_record_time = timezone.make_aware(new_last_record_time)
                    except (ValueError, TypeError):
                        logger.warning(f"Failed to parse last_record_time: {new_last_record_time_str}")
                
                existing_report = DailyReport.objects.filter(
                    character=character,
                    date=target_date
                ).first()
                
                is_final_summary = (target_date < today)
                
                if existing_report:
                    existing_last_record_time = existing_report.last_record_time
                    
                    has_new_data = True
                    if existing_last_record_time and new_last_record_time:
                        if new_last_record_time <= existing_last_record_time:
                            has_new_data = False
                    
                    if not has_new_data and not is_final_summary:
                        logger.info(f"No new data for {character.name} on {target_date} since {existing_last_record_time}, skipping LLM analysis")
                        skipped_count += 1
                        continue
                    
                    if is_final_summary and not has_new_data:
                        logger.info(f"Triggering final LLM summary for {character.name} on {target_date} despite no new data")
                    else:
                        logger.info(f"New data found for {character.name} on {target_date}, updating report")
                    
                    use_incremental = (target_date == today) and not is_final_summary
                    previous_report = existing_report.analysis_result.get('markdown', '') if existing_report.analysis_result else ''
                    previous_cutoff_time = existing_report.data_cutoff_time
                    memory_context = ''
                    if is_final_summary:
                        memory_context = format_events_for_prompt(
                            retrieve_important_events(character, aggregated_data)
                        )
                    
                    # 准备人设信息
                    persona_info = {
                        "persona": config.persona,
                        "persona_updated_at": config.persona_updated_at,
                        "ai_persona": config.ai_persona,
                        "system_inferred_persona": config.system_inferred_persona,
                    }

                    # 准备增量保存回调
                    def on_module_complete_callback(current_sections):
                        if existing_report:
                            res = existing_report.analysis_result or {"version": 2, "sections": {}}
                            res['sections'] = current_sections
                            existing_report.analysis_result = res
                            existing_report.save(update_fields=['analysis_result'])
                            logger.info(f"  [Incremental] Progress saved for {character.name}")

                    analysis_result = analyze_all_modules_sequential(
                        aggregated_data,
                        character.name,
                        persona_info,
                        previous_analysis_result=existing_report.analysis_result,
                        long_term_memory_context=memory_context,
                        on_module_complete=on_module_complete_callback,
                        is_day_ended=is_final_summary,
                        incremental=True
                    )
                    
                    if 'error' in analysis_result:
                        raise Exception(f"LLM Analysis failed: {analysis_result['error']}")
                    
                    existing_report.raw_data = aggregated_data
                    existing_report.analysis_result = analysis_result
                    existing_report.last_record_time = new_last_record_time
                    existing_report.data_cutoff_time = current_cutoff_time
                    existing_report.save()
                    
                    updated_count += 1
                    success_count += 1
                    logger.info(f"Successfully updated report for {character.name} on {target_date}")
                    
                else:
                    logger.info(f"No existing report for {character.name} on {target_date}, creating new report")
                    memory_context = ''
                    if is_final_summary:
                        memory_context = format_events_for_prompt(
                            retrieve_important_events(character, aggregated_data)
                        )
                    
                    # 准备人设信息
                    persona_info = {
                        "persona": config.persona,
                        "persona_updated_at": config.persona_updated_at,
                        "ai_persona": config.ai_persona,
                        "system_inferred_persona": config.system_inferred_persona,
                    }

                    # 如果是新日报，先创建一个占位记录，以便支持增量保存展示
                    existing_report = DailyReport.objects.create(
                        character=character,
                        date=target_date,
                        is_hidden=False,
                        raw_data=aggregated_data,
                        analysis_result={"version": 2, "sections": {}},
                        last_record_time=new_last_record_time,
                        data_cutoff_time=current_cutoff_time
                    )

                    # 准备增量保存回调
                    def on_module_complete_callback(current_sections):
                        if existing_report:
                            res = existing_report.analysis_result or {"version": 2, "sections": {}}
                            res['sections'] = current_sections
                            existing_report.analysis_result = res
                            existing_report.save(update_fields=['analysis_result'])
                            logger.info(f"  [Incremental] Progress saved for {character.name}")

                    analysis_result = analyze_all_modules_sequential(
                        aggregated_data,
                        character.name,
                        persona_info,
                        previous_analysis_result=None,
                        long_term_memory_context=memory_context,
                        on_module_complete=on_module_complete_callback,
                        is_day_ended=is_final_summary,
                        incremental=True
                    )
                    
                    if 'error' in analysis_result:
                        raise Exception(f"LLM Analysis failed: {analysis_result['error']}")
                    
                    existing_report.analysis_result = analysis_result
                    existing_report.save()
                    
                    new_count += 1
                    success_count += 1
                    logger.info(f"Successfully created report for {character.name} on {target_date}")
                    
            except Exception as e:
                failed_count += 1
                logger.error(f"Failed to generate report for character {config.character.name if config.character else 'unknown'} on {target_date}: {str(e)}")
                import traceback
                logger.error(traceback.format_exc())
                continue
    
    logger.info(
        f"Daily report generation completed. "
        f"Success: {success_count} (New: {new_count}, Updated: {updated_count}), "
        f"Skipped: {skipped_count}, Failed: {failed_count}"
    )
    
    return {
        'date': today.isoformat(),
        'data_cutoff_time': local_now.isoformat(),
        'success_count': success_count,
        'new_count': new_count,
        'updated_count': updated_count,
        'skipped_count': skipped_count,
        'failed_count': failed_count,
        'total_processed': active_configs.count()
    } 


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=600,
    autoretry_for=(Exception,),
    retry_backoff=True,
    # 强制超时保护
    soft_time_limit=1200,  # 20分钟
    time_limit=1500,       # 25分钟
)
def generate_important_event_memories(self):
    """
    每天 00:30 从前一天最终日报中抽取长期重要事件。

    日报生成仍以 raw_data 为事实来源；analysis_result.markdown 仅作为辅助线索。
    """
    local_now = timezone.localtime(timezone.now())
    target_date = local_now.date() - timedelta(days=1)

    active_configs = DailyReportConfig.objects.filter(
        is_enabled=True
    ).select_related('character')

    success_count = 0
    failed_count = 0
    skipped_count = 0
    created_count = 0
    updated_count = 0

    logger.info(f"Starting important event memory generation for {target_date}")

    for config in active_configs:
        try:
            report = DailyReport.objects.filter(
                character=config.character,
                date=target_date,
            ).select_related('character').first()

            if not report:
                logger.info(f"No daily report found for {config.character.name} on {target_date}, skip memory extraction")
                skipped_count += 1
                continue

            result = extract_important_events_for_report(report)
            if result.get('skipped'):
                skipped_count += 1
            else:
                success_count += 1
                created_count += result.get('created', 0)
                updated_count += result.get('updated', 0)

        except Exception as e:
            failed_count += 1
            logger.error(f"Failed to generate important event memory for {config.character.name} on {target_date}: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            continue

    logger.info(
        f"Important event memory generation completed. "
        f"Success: {success_count}, Created: {created_count}, Updated: {updated_count}, "
        f"Skipped: {skipped_count}, Failed: {failed_count}"
    )

    return {
        'date': target_date.isoformat(),
        'success_count': success_count,
        'created_count': created_count,
        'updated_count': updated_count,
        'skipped_count': skipped_count,
        'failed_count': failed_count,
        'total_processed': active_configs.count(),
    }


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=600,
    autoretry_for=(Exception,),
    retry_backoff=True,
    soft_time_limit=1200,
    time_limit=1500,
)
def update_personas(self):
    """
    每天 00:20 执行，分析过去 7 天数据更新系统侧写 (System Persona)。
    """
    local_now = timezone.localtime(timezone.now())
    today = local_now.date()
    
    active_configs = DailyReportConfig.objects.filter(
        is_enabled=True
    ).select_related('character')

    success_count = 0
    failed_count = 0

    logger.info(f"Starting scheduled persona update at {local_now.isoformat()}")

    for config in active_configs:
        try:
            logger.info(f"Updating persona for character: {config.character.name}")
            update_system_persona(config, today=today)
            success_count += 1
        except Exception as e:
            failed_count += 1
            logger.error(f"Failed to update persona for {config.character.name}: {str(e)}")
            continue

    logger.info(f"Scheduled persona update completed. Success: {success_count}, Failed: {failed_count}")
    return {
        'date': today.isoformat(),
        'success_count': success_count,
        'failed_count': failed_count,
        'total_processed': active_configs.count()
    }


@shared_task
def sync_unsynced_events_task():
    """
    定期（如每 6 小时）补漏：同步所有未成功进入 Milvus 的重要事件。
    """
    from apps.characters.models import ImportantEvent
    from apps.characters.services.important_event_service import sync_events_to_milvus
    
    unsynced_events = ImportantEvent.objects.filter(
        milvus_synced=False, 
        is_active=True
    ).order_by('-date')[:100] # 每次最多处理 100 条，避免压力过大
    
    if not unsynced_events.exists():
        return "No unsynced events found."
        
    count = unsynced_events.count()
    logger.info(f"Periodic sync starting: found {count} unsynced events.")
    
    # 利用我们新写的批量同步逻辑
    synced_count = sync_events_to_milvus(list(unsynced_events))
    
    logger.info(f"Periodic sync completed: {synced_count}/{count} events synced.")
    return f"Synced {synced_count}/{count} events."
