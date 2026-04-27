from celery import shared_task
from django.utils import timezone
from datetime import timedelta, date, datetime
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.conf import settings
from .models import WillConfig, CharacterStatus, DailyReportConfig, DailyReport, Character
import logging
import os
import json
from django.db import transaction
from collections import defaultdict, Counter

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


def aggregate_status_data(character, field_mappings, target_date):
    """
    聚合指定日期的状态数据
    
    Args:
        character: 角色对象
        field_mappings: 字段映射，格式: {"phone_app": "key", "computer_app": "key", "steps": "key"}
        target_date: 目标日期
    
    Returns:
        dict: 聚合后的数据
    """
    start_datetime = timezone.make_aware(datetime.combine(target_date, datetime.min.time()))
    end_datetime = start_datetime + timedelta(days=1)
    
    statuses = CharacterStatus.objects.filter(
        character=character,
        timestamp__gte=start_datetime,
        timestamp__lt=end_datetime
    ).order_by('timestamp')
    
    if not statuses.exists():
        return None
    
    aggregated = {
        'date': target_date.isoformat(),
        'total_records': statuses.count(),
        'phone_app_usage': [],
        'computer_app_usage': [],
        'steps_data': [],
        'all_statuses': []
    }
    
    phone_key = field_mappings.get('phone_app')
    computer_key = field_mappings.get('computer_app')
    steps_key = field_mappings.get('steps')
    
    for status in statuses:
        status_data = {
            'timestamp': status.timestamp.isoformat(),
            'status_type': status.status_type,
            'data': status.data
        }
        aggregated['all_statuses'].append(status_data)
        
        data = status.data
        hour = status.timestamp.hour
        
        if phone_key and phone_key in data:
            value = data[phone_key]
            if value:
                aggregated['phone_app_usage'].append({
                    'hour': hour,
                    'timestamp': status.timestamp.isoformat(),
                    'app': str(value)
                })
        
        if computer_key and computer_key in data:
            value = data[computer_key]
            if value:
                aggregated['computer_app_usage'].append({
                    'hour': hour,
                    'timestamp': status.timestamp.isoformat(),
                    'app': str(value)
                })
        
        if steps_key and steps_key in data:
            try:
                steps = int(data[steps_key])
                aggregated['steps_data'].append({
                    'hour': hour,
                    'timestamp': status.timestamp.isoformat(),
                    'steps': steps
                })
            except (ValueError, TypeError):
                pass
    
    if aggregated['phone_app_usage']:
        phone_counter = Counter(item['app'] for item in aggregated['phone_app_usage'])
        aggregated['phone_app_summary'] = dict(phone_counter.most_common(20))
        
        hourly_phone = defaultdict(list)
        for item in aggregated['phone_app_usage']:
            hourly_phone[item['hour']].append(item['app'])
        aggregated['phone_app_by_hour'] = {
            str(hour): dict(Counter(apps).most_common(5))
            for hour, apps in hourly_phone.items()
        }
    
    if aggregated['computer_app_usage']:
        computer_counter = Counter(item['app'] for item in aggregated['computer_app_usage'])
        aggregated['computer_app_summary'] = dict(computer_counter.most_common(20))
        
        hourly_computer = defaultdict(list)
        for item in aggregated['computer_app_usage']:
            hourly_computer[item['hour']].append(item['app'])
        aggregated['computer_app_by_hour'] = {
            str(hour): dict(Counter(apps).most_common(5))
            for hour, apps in hourly_computer.items()
        }
    
    if aggregated['steps_data']:
        steps_values = [item['steps'] for item in aggregated['steps_data']]
        aggregated['steps_summary'] = {
            'min': min(steps_values) if steps_values else 0,
            'max': max(steps_values) if steps_values else 0,
            'last': steps_values[-1] if steps_values else 0
        }
        
        hourly_steps = defaultdict(list)
        for item in aggregated['steps_data']:
            hourly_steps[item['hour']].append(item['steps'])
        aggregated['steps_by_hour'] = {
            str(hour): max(steps) if steps else 0
            for hour, steps in hourly_steps.items()
        }
    
    active_hours = set()
    for status in statuses:
        active_hours.add(status.timestamp.hour)
    aggregated['active_hours'] = sorted(list(active_hours))
    
    if aggregated['active_hours']:
        aggregated['first_activity_hour'] = min(aggregated['active_hours'])
        aggregated['last_activity_hour'] = max(aggregated['active_hours'])
    
    return aggregated


def analyze_with_llm(aggregated_data, character_name):
    """
    使用 Anthropic API 分析数据
    
    Args:
        aggregated_data: 聚合后的数据
        character_name: 角色名称
    
    Returns:
        dict: AI 分析结果，包含 'markdown' 字段
    """
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    model = getattr(settings, 'ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')
    base_url = getattr(settings, 'ANTHROPIC_BASE_URL', None)
    
    if not api_key:
        logger.warning("Anthropic API key not configured, skipping LLM analysis")
        return {
            'markdown': '## 分析失败\n\n由于 API 未配置，无法进行 AI 分析。',
            'error': 'Anthropic API key not configured'
        }
    
    try:
        import anthropic
        
        client_kwargs = {'api_key': api_key}
        if base_url:
            client_kwargs['base_url'] = base_url
        
        client = anthropic.Anthropic(**client_kwargs)
        
        data_summary = {
            'date': aggregated_data.get('date'),
            'total_records': aggregated_data.get('total_records', 0),
            'active_hours': aggregated_data.get('active_hours', []),
            'first_activity_hour': aggregated_data.get('first_activity_hour'),
            'last_activity_hour': aggregated_data.get('last_activity_hour'),
            'phone_app_summary': aggregated_data.get('phone_app_summary', {}),
            'computer_app_summary': aggregated_data.get('computer_app_summary', {}),
            'phone_app_by_hour': aggregated_data.get('phone_app_by_hour', {}),
            'computer_app_by_hour': aggregated_data.get('computer_app_by_hour', {}),
            'steps_summary': aggregated_data.get('steps_summary', {})
        }
        
        prompt = f"""你是一位专业的生活数据分析助手。请根据以下数据分析用户 {character_name} 在 {data_summary['date']} 的活动情况。

数据概览：
- 总记录数: {data_summary['total_records']}
- 活动小时: {data_summary['active_hours']}
- 首次活动时间: {data_summary.get('first_activity_hour', '未知')} 点
- 最后活动时间: {data_summary.get('last_activity_hour', '未知')} 点
"""
        
        if data_summary['phone_app_summary']:
            prompt += f"""
手机应用使用统计（按使用次数）:
{json.dumps(data_summary['phone_app_summary'], ensure_ascii=False, indent=2)}

手机应用按小时分布:
{json.dumps(data_summary['phone_app_by_hour'], ensure_ascii=False, indent=2)}
"""
        
        if data_summary['computer_app_summary']:
            prompt += f"""
电脑应用使用统计（按使用次数）:
{json.dumps(data_summary['computer_app_summary'], ensure_ascii=False, indent=2)}

电脑应用按小时分布:
{json.dumps(data_summary['computer_app_by_hour'], ensure_ascii=False, indent=2)}
"""
        
        if data_summary['steps_summary']:
            prompt += f"""
步数统计:
- 最大步数: {data_summary['steps_summary'].get('max', 0)}
- 最后记录步数: {data_summary['steps_summary'].get('last', 0)}
"""
        
        prompt += """
请以 Markdown 格式输出分析报告，包含以下部分：

## 活动总结
简要总结用户当天的主要活动（100-200字）。

## 作息规律
分析用户的作息时间，包括：
- **起床时间估计**: 根据首次活动时间推断
- **入睡时间估计**: 根据最后活动时间推断
- **活跃时段**: 列出主要活跃的时间段
- **常用应用**: 列出使用最多的应用

## 异常活动
如果检测到异常活动（如凌晨 0:00-5:00 的活动、反常的应用使用等），请列出每个异常的时间和描述。如果没有异常活动，则说明"今日无异常活动"。

注意事项：
- 所有时间以北京时间为准
- 分析要基于实际数据，如果数据不足，请合理推断或说明
- 使用中文输出
- 直接输出 Markdown 内容，不要有其他说明文字"""

        response = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )
        
        if not response.content or len(response.content) == 0:
            logger.error("LLM response is empty")
            return {
                'markdown': '## 分析失败\n\nAI 返回了空响应。',
                'error': 'LLM response is empty'
            }
        
        logger.info(f"Total content blocks: {len(response.content)}")
        
        result_text = None
        thinking_content = None
        
        for i, block in enumerate(response.content):
            block_type = getattr(block, 'type', 'unknown')
            logger.info(f"Block {i}: type={block_type}")
            logger.info(f"Block {i} full: {block}")
            
            if hasattr(block, 'model_dump'):
                try:
                    dumped = block.model_dump()
                    logger.info(f"Block {i} model_dump: {dumped}")
                except Exception as e:
                    logger.info(f"Block {i} model_dump failed: {e}")
            
            if block_type == 'text':
                if hasattr(block, 'text') and block.text is not None:
                    result_text = block.text
                    logger.info(f"Found text block {i}, text length: {len(result_text)}")
                    break
            
            if block_type == 'thinking':
                if hasattr(block, 'thinking') and block.thinking is not None:
                    thinking_content = block.thinking
                    logger.info(f"Found thinking block {i}, thinking length: {len(thinking_content)}")
        
        if result_text is None:
            logger.warning("No text block found, checking for alternative extraction methods")
            
            for i, block in enumerate(response.content):
                if hasattr(block, 'text') and block.text is not None:
                    result_text = block.text
                    logger.info(f"Found text via text attribute in block {i}")
                    break
                
                if hasattr(block, 'thinking') and block.thinking is not None:
                    if thinking_content is None:
                        thinking_content = block.thinking
        
        if result_text is None:
            try:
                str_content = str(response.content[-1])
                logger.info(f"Using str() of last block: {str_content[:200]}...")
                if str_content and len(str_content.strip()) > 0:
                    if 'text=' in str_content and 'text=None' not in str_content:
                        import re
                        match = re.search(r"text='([^']+)'", str_content)
                        if match:
                            result_text = match.group(1)
                            logger.info(f"Extracted text from str(): {len(result_text)} chars")
            except Exception as e:
                logger.info(f"Failed to extract from str(): {e}")
        
        if result_text is None or not isinstance(result_text, str) or len(result_text.strip()) == 0:
            logger.error(f"Failed to extract text from any block. Response: {response.content}")
            return {
                'markdown': '## 分析失败\n\nAI 返回了无效的响应格式。',
                'error': 'Failed to extract text from response'
            }
        
        logger.info(f"Successfully extracted text, length: {len(result_text)}")
        
        return {
            'markdown': result_text
        }
        
    except ImportError:
        logger.error("Anthropic SDK not installed")
        return {
            'markdown': '## 分析失败\n\n由于依赖未安装，无法进行 AI 分析。',
            'error': 'Anthropic SDK not installed'
        }
    except Exception as e:
        logger.error(f"LLM analysis failed: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'markdown': f'## 分析失败\n\n分析过程中发生错误：{str(e)}',
            'error': str(e)
        }


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=600,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def generate_daily_reports(self):
    """
    定时生成日报分析
    
    每天凌晨执行，分析前一天所有启用了日报分析功能的角色数据
    """
    now = timezone.now()
    target_date = now.date() - timedelta(days=1)
    
    logger.info(f"Starting daily report generation for {target_date.isoformat()}")
    
    active_configs = DailyReportConfig.objects.filter(
        is_enabled=True
    ).select_related('character')
    
    logger.info(f"Found {active_configs.count()} characters with daily report enabled")
    
    success_count = 0
    failed_count = 0
    
    for config in active_configs:
        try:
            character = config.character
            logger.info(f"Processing report for character: {character.name} (uid: {character.uid})")
            
            existing_report = DailyReport.objects.filter(
                character=character,
                date=target_date
            ).first()
            
            if existing_report:
                logger.info(f"Report already exists for {character.name} on {target_date}, skipping")
                continue
            
            field_mappings = config.field_mappings or {}
            
            if not field_mappings:
                logger.warning(f"No field mappings configured for {character.name}, skipping report")
                continue
            
            aggregated_data = aggregate_status_data(character, field_mappings, target_date)
            
            if not aggregated_data:
                logger.info(f"No status data found for {character.name} on {target_date}")
                continue
            
            analysis_result = analyze_with_llm(aggregated_data, character.name)
            
            DailyReport.objects.create(
                character=character,
                date=target_date,
                is_hidden=False,
                raw_data=aggregated_data,
                analysis_result=analysis_result
            )
            
            success_count += 1
            logger.info(f"Successfully generated report for {character.name}")
            
        except Exception as e:
            failed_count += 1
            logger.error(f"Failed to generate report for character {config.character.name if config.character else 'unknown'}: {str(e)}")
            continue
    
    logger.info(f"Daily report generation completed. Success: {success_count}, Failed: {failed_count}")
    
    return {
        'date': target_date.isoformat(),
        'success_count': success_count,
        'failed_count': failed_count,
        'total_processed': active_configs.count()
    } 