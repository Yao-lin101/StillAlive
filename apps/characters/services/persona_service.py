import json
import logging
from django.utils import timezone
from django.conf import settings
from apps.characters.models import DailyReport, PersonaHistory
from .llm_service import extract_text_from_anthropic_response

logger = logging.getLogger(__name__)

def _get_raw_data_summary(report):
    """
    从日报中提取客观的原始数据摘要（用于侧写更新）
    避免使用已被风格化和侧写影响的 markdown
    """
    if not report.raw_data:
        return '暂无数据'
        
    weekday_str = ""
    if report.date:
        try:
            weekday_map = {0: '星期一', 1: '星期二', 2: '星期三', 3: '星期四', 4: '星期五', 5: '星期六', 6: '星期日'}
            weekday_str = weekday_map[report.date.weekday()]
        except Exception:
            pass

    return json.dumps({
        'weekday': weekday_str,
        'total_records': report.raw_data.get('total_records'),
        'active_hours': report.raw_data.get('active_hours'),
        'first_activity_hour': report.raw_data.get('first_activity_hour'),
        'last_activity_hour': report.raw_data.get('last_activity_hour'),
        'phone_app_summary': report.raw_data.get('phone_app_summary'),
        'computer_app_summary': report.raw_data.get('computer_app_summary'),
        'steps_summary': report.raw_data.get('steps_summary')
    }, ensure_ascii=False)



def update_system_persona(config, yesterday_report_text=None, trigger_type='scheduled', today=None):
    """
    使用 Anthropic API 更新系统的暗中认知人设
    
    重要设计原则：
    1. 始终使用【原始客观数据】（raw_data）而非风格化日报（markdown）
       避免"侧写影响日报，日报又影响侧写"的自证预言循环
    2. 始终使用【7天数据窗口】（首次和更新都一样）
       让 LLM 能真正观察到长期趋势，避免单天数据被当作"反常"
    3. 记录历史：每次更新都会保存到 PersonaHistory 表（每天一条，多次更新则覆盖）
    
    注意：yesterday_report_text 参数已不再使用，仅保留以保持向后兼容。
          现在始终从数据库获取最近 7 天的原始数据进行分析。
    """
    from apps.characters.models import DailyReport, PersonaHistory
    
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    model = getattr(settings, 'ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')
    base_url = getattr(settings, 'ANTHROPIC_BASE_URL', None)
    
    if today is None:
        today = timezone.localdate()
    
    if not api_key:
        return
        
    try:
        import anthropic
        client_kwargs = {'api_key': api_key}
        if base_url:
            client_kwargs['base_url'] = base_url
        client = anthropic.Anthropic(**client_kwargs)
        
        is_first_time = not config.system_inferred_persona
        
        recent_reports = DailyReport.objects.filter(
            character=config.character,
            is_hidden=False
        ).order_by('-date')[:7]
        
        if recent_reports.count() < 3:
            logger.info(f"数据不足 3 天（当前 {recent_reports.count()} 天），暂不生成/更新侧写档案，以免产生偏差。")
            return False
        
        data_dates = [str(r.date) for r in reversed(recent_reports)]
        
        reports_text = "\n\n---\n\n".join([
            f"日期：{r.date}\n客观活动聚合数据：{_get_raw_data_summary(r)}" 
            for r in reversed(recent_reports)
        ])
        
        if is_first_time:
            data_section = f"这是你第一次对该用户进行侧写。为了防止受到用户过去自述人设的误导，以下直接提供该用户过去几天（最多7天）的【纯客观活动聚合数据 JSON】：\n{reports_text}\n\n请完全基于这些无滤镜的客观数据，穿透表象，总结出他初始的真实侧写档案。"
        else:
            data_section = f"以下是该用户过去几天（最多7天）的【纯客观活动聚合数据 JSON】（按时间顺序）：\n{reports_text}\n\n请基于这些客观数据，结合你之前的侧写档案，更新他的真实侧写档案。"

        system_prompt = "你是一个极度冷酷、尖锐的心理与行为侧写师，负责通过观察一个人的日常活动记录，暗中推断他的真实人设。"
        
        if is_first_time:
            trend_guidance = """1. 寻找模式：从多天的数据中识别稳定的行为模式和核心特征。"""
        else:
            trend_guidance = """1. 观察趋势：从多天的数据中识别【正在发生的变化趋势】。
   - 如果是持续的、多天一致的变化（如连续早睡），请更新侧写以反映真实状态。
   - 如果只是单天反常（如某天突然早睡但其他时候都是夜猫子），请保持原有警惕（比如："虽然今天早睡了，但大概率只是通宵后的补觉"）。
   - 关键：你有 7 天数据，不要把任何变化都当作"单独一天的反常"，要判断这是趋势还是噪音。"""
        
        user_prompt = f"""以下是该用户自己声称的人设背景：
{config.persona or "（无）"}

以下是你上次对他进行的暗中侧写档案：
{config.system_inferred_persona or "（这是第一次评估，暂无历史侧写）"}

{data_section}

【重要要求】：
{trend_guidance}
2. 揭穿谎言：如果实际行为严重打脸了他"自己声称的人设"，请在侧写中毫不留情地将其标记为"假装努力"或"自欺欺人"。
3. 高度抽象：侧写档案必须是对其性格、真实身份、生活状态的【宏观定性总结】（如"昼夜颠倒的赛博劳工"、"假装躺平实则焦虑找工作"）。**绝对不允许在侧写中罗列具体日期（如"4月23日"）或具体的数据（如"用了16次"）**，你是提炼核心特征，不是在写财务报表！
4. 严格精简：不要寒暄，不需要解释过程，直接输出最终的侧写档案，务必控制在 150 字以内，字字诛心。"""
        
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            temperature=0.4,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        
        new_persona = extract_text_from_anthropic_response(response)
        
        if new_persona:
            new_persona = new_persona.strip()
            config.system_inferred_persona = new_persona
            config.save(update_fields=['system_inferred_persona'])
            logger.info(f"Successfully updated system_inferred_persona for {config.character.name}")
            
            PersonaHistory.objects.update_or_create(
                config=config,
                date=today,
                defaults={
                    'persona_content': new_persona,
                    'data_dates': data_dates,
                    'is_first_time': is_first_time,
                    'model_used': model,
                    'trigger_type': trigger_type,
                }
            )
            logger.info(f"Persona history recorded for {config.character.name} on {today}")
        else:
            logger.error("Failed to extract text from LLM response for persona update.")
        
    except Exception as e:
        logger.error(f"Failed to update system_inferred_persona: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())


