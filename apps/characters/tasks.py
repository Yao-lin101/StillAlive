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


def aggregate_status_data(character, field_mappings, target_date, end_datetime=None):
    """
    聚合指定日期的状态数据
    
    Args:
        character: 角色对象
        field_mappings: 字段映射，格式: {"phone_app": "key", "computer_app": "key", "steps": "key"}
        target_date: 目标日期
        end_datetime: 数据截止时间（可选，默认是当天结束）
    
    Returns:
        dict: 聚合后的数据，包含 'last_record_time' 字段（最新状态的时间）
    """
    start_datetime = timezone.make_aware(datetime.combine(target_date, datetime.min.time()))
    
    if end_datetime is None:
        end_datetime = start_datetime + timedelta(days=1)
    
    statuses = CharacterStatus.objects.filter(
        character=character,
        timestamp__gte=start_datetime,
        timestamp__lt=end_datetime
    ).order_by('timestamp')
    
    if not statuses.exists():
        return None
    
    latest_status = statuses.last()
    
    aggregated = {
        'date': target_date.isoformat(),
        'total_records': statuses.count(),
        'last_record_time': timezone.localtime(latest_status.timestamp).isoformat() if latest_status else None,
        'data_cutoff_time': timezone.localtime(end_datetime).isoformat() if timezone.is_aware(end_datetime) else end_datetime.isoformat(),
        'phone_app_usage': [],
        'computer_app_usage': [],
        'steps_data': [],
        'all_statuses': []
    }
    
    phone_key = field_mappings.get('phone_app')
    computer_key = field_mappings.get('computer_app')
    steps_key = field_mappings.get('steps')
    
    for status in statuses:
        local_timestamp = timezone.localtime(status.timestamp)
        
        status_data = {
            'timestamp': local_timestamp.isoformat(),
            'status_type': status.status_type,
            'data': status.data
        }
        aggregated['all_statuses'].append(status_data)
        
        data = status.data
        hour = local_timestamp.hour
        
        if phone_key and phone_key in data:
            value = data[phone_key]
            if value:
                aggregated['phone_app_usage'].append({
                    'hour': hour,
                    'timestamp': local_timestamp.isoformat(),
                    'app': str(value)
                })
        
        if computer_key and computer_key in data:
            value = data[computer_key]
            if value:
                aggregated['computer_app_usage'].append({
                    'hour': hour,
                    'timestamp': local_timestamp.isoformat(),
                    'app': str(value)
                })
        
        if steps_key and steps_key in data:
            try:
                steps = int(data[steps_key])
                aggregated['steps_data'].append({
                    'hour': hour,
                    'timestamp': local_timestamp.isoformat(),
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
            'total': steps_values[-1] if steps_values else 0
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
        active_hours.add(timezone.localtime(status.timestamp).hour)
    aggregated['active_hours'] = sorted(list(active_hours))
    
    if aggregated['active_hours']:
        aggregated['first_activity_hour'] = min(aggregated['active_hours'])
        aggregated['last_activity_hour'] = max(aggregated['active_hours'])
    
    return aggregated


def analyze_with_llm(aggregated_data, character_name, persona=None):
    """
    使用 Anthropic API 分析数据
    
    Args:
        aggregated_data: 聚合后的数据
        character_name: 角色名称
        persona: 角色人设信息（可选）
    
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
            'steps_summary': aggregated_data.get('steps_summary', {}),
            'steps_by_hour': aggregated_data.get('steps_by_hour', {}),
            'last_record_time': aggregated_data.get('last_record_time'),
            'data_cutoff_time': aggregated_data.get('data_cutoff_time')
        }
        
        system_prompt = """你是一位毒舌但精准的生活数据分析专家，负责对用户的日常活动数据进行锐评式分析。

## 硬性约束
1. 语言风格：毒舌、尖锐、抽象、有梗。口语化，可适当使用网络流行语。多用 emoji 增加表现力（如🌙🌅🤔🤡💀📱）。
2. 分析原则：基于实际数据进行合理怀疑与大胆推测（使用"可能"、"难道是"等词），但绝不凭空捏造。
3. 数据局限性：
   - 只能获取**前台运行**的应用状态，无法获取后台状态。
   - 某个应用（如音乐、下载、视频）在数据中只出现一次，可能意味着它一直在后台运行。不要错误推断"只使用了一次"或"只用了几分钟"。
   - 步数是全天累计值（按小时分布的数据表示"截至该小时的总步数"），切勿将其误解为"单独某个小时走出的步数"然后进行累加计算。
   - 若"数据截止时间"未满24小时，说明当天尚未结束，切勿将"最后活动时间"误判为用户已睡觉或收工。
4. 时区：所有时间均为北京时间。
"""

        user_prompt = f"请对用户 {character_name} 在 {data_summary['date']} 的活动进行分析。\n"
        
        if persona and persona.strip():
            user_prompt += f"\n## 角色背景\n{persona.strip()}\n请结合上述人设背景进行分析，使锐评更贴合角色。\n"

        user_prompt += f"""
## 数据概览
- 总记录数: {data_summary['total_records']}
- 活动小时: {data_summary['active_hours']}
- 首次活动时间: {data_summary.get('first_activity_hour', '未知')} 点
- 最后活动时间: {data_summary.get('last_activity_hour', '未知')} 点
- 数据截止时间: {data_summary.get('data_cutoff_time', '未知')}
"""
        
        if data_summary['phone_app_summary']:
            user_prompt += f"\n## 手机应用（总计前20）\n{json.dumps(data_summary['phone_app_summary'], ensure_ascii=False)}\n"
            user_prompt += f"\n## 手机应用（按小时）\n{json.dumps(data_summary['phone_app_by_hour'], ensure_ascii=False)}\n"
            
        if data_summary['computer_app_summary']:
            user_prompt += f"\n## 电脑应用（总计前20）\n{json.dumps(data_summary['computer_app_summary'], ensure_ascii=False)}\n"
            user_prompt += f"\n## 电脑应用（按小时）\n{json.dumps(data_summary['computer_app_by_hour'], ensure_ascii=False)}\n"
            
        if data_summary['steps_summary']:
            user_prompt += f"\n## 今日总步数: {data_summary['steps_summary'].get('total', 0)}\n"
            if data_summary.get('steps_by_hour'):
                user_prompt += f"\n## 步数（按小时累计）\n{json.dumps(data_summary['steps_by_hour'], ensure_ascii=False)}\n"
            
        user_prompt += """
## 输出格式
请直接输出 Markdown 格式，不要包含任何说明文字：

```markdown
# [2-4字短评，如：修仙模式/不知所踪/平平无奇]
[1-2句话总结今日整体活动，需尖锐、有冲击力]

## 作息诊断
[2-3句话锐评作息时间，是人类、夜猫子还是修仙者？]

## 活动画像
[2-3句话从应用使用情况推测用户当前的状态（如摸鱼/学习/社交等）]

## 合理怀疑
[2-3句话寻找反常时间点或行为进行推测。若无反常则评"平平无奇"]
```
"""

        logger.info("=== LLM Analysis Prompt Start ===")
        logger.info(f"System Prompt:\n{system_prompt}")
        logger.info(f"User Prompt:\n{user_prompt}")
        logger.info("=== LLM Analysis Prompt End ===")

        response = client.messages.create(
            model=model,
            system=system_prompt,
            temperature=0.8,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": user_prompt
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
        
        # 清理 LLM 回复格式
        # 情况 1：被 ```markdown 或 ``` 包裹
        import re
        
        # 检测是否被 ```markdown 或 ``` 包裹
        # 匹配格式：```markdown\n...\n``` 或 ```\n...\n```
        result_text = result_text.strip()
        
        # 情况 1：被 ```markdown 包裹
        markdown_match = re.match(
            r'^```markdown\s*\n(.*?)\n```\s*$',
            result_text,
            re.DOTALL
        )
        if markdown_match:
            result_text = markdown_match.group(1).strip()
            logger.info("Removed ```markdown code block wrapper")
        
        # 情况 2：被 ``` 包裹（不带 markdown 标签）
        else:
            code_block_match = re.match(
                r'^```\s*\n(.*?)\n```\s*$',
                result_text,
                re.DOTALL
            )
            if code_block_match:
                result_text = code_block_match.group(1).strip()
                logger.info("Removed ``` code block wrapper")
        
        # 情况 3：开头有 ```markdown 或 ``` 但结尾没有（不完整的代码块）
        if result_text.startswith('```markdown'):
            result_text = result_text[len('```markdown'):].strip()
            if result_text.startswith('\n'):
                result_text = result_text[1:].strip()
            logger.info("Removed leading ```markdown")
        
        elif result_text.startswith('```'):
            result_text = result_text[3:].strip()
            if result_text.startswith('\n'):
                result_text = result_text[1:].strip()
            logger.info("Removed leading ```")
        
        # 情况 4：结尾有 ```
        if result_text.endswith('```'):
            result_text = result_text[:-3].strip()
            logger.info("Removed trailing ```")
        
        logger.info(f"Cleaned text length: {len(result_text)}")
        
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
                
                if existing_report:
                    existing_last_record_time = existing_report.last_record_time
                    
                    has_new_data = True
                    if existing_last_record_time and new_last_record_time:
                        if new_last_record_time <= existing_last_record_time:
                            has_new_data = False
                            logger.info(f"No new data for {character.name} on {target_date} since {existing_last_record_time}, skipping LLM analysis")
                    
                    if not has_new_data:
                        skipped_count += 1
                        continue
                    
                    logger.info(f"New data found for {character.name} on {target_date}, updating report")
                    
                    analysis_result = analyze_with_llm(aggregated_data, character.name, config.persona)
                    
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
                    
                    analysis_result = analyze_with_llm(aggregated_data, character.name, config.persona)
                    
                    DailyReport.objects.create(
                        character=character,
                        date=target_date,
                        is_hidden=False,
                        raw_data=aggregated_data,
                        analysis_result=analysis_result,
                        last_record_time=new_last_record_time,
                        data_cutoff_time=current_cutoff_time
                    )
                    
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