import json
import logging
from django.utils import timezone
from django.conf import settings
from .prompts import (
    DEFAULT_SYSTEM_PROMPT, CUSTOM_SYSTEM_RULES_APPENDIX,
    INCREMENTAL_UPDATE_PROMPT, FINAL_SUMMARY_PROMPT,
    CUSTOM_FORMAT_INSTRUCTIONS, DEFAULT_FORMAT_INSTRUCTIONS,
    CUSTOM_QQ_FORMAT_SECTION, DEFAULT_QQ_FORMAT_SECTION
)
from .data_service import ACTIVE_INTERVAL_MAX_GAP

logger = logging.getLogger(__name__)

def extract_text_from_anthropic_response(response):
    """
    安全地从 Anthropic API 的 response 中提取文本，兼容带有 thinking block 的模型。
    """
    import re
    result_text = None
    thinking_content = None
    
    if hasattr(response, 'content'):
        text_blocks = []
        for i, block in enumerate(response.content):
            block_type = getattr(block, 'type', 'unknown')
            
            if block_type == 'text':
                if hasattr(block, 'text') and block.text is not None:
                    text_blocks.append(block.text)
            
            if block_type == 'thinking':
                if hasattr(block, 'thinking') and block.thinking is not None:
                    thinking_content = block.thinking
                    
        if text_blocks:
            # 代理 API 可能会将推理过程作为第一个 text block，将最终回复作为最后一个 text block
            result_text = text_blocks[-1]
        
        if result_text is None:
            try:
                str_content = str(response.content[-1])
                if str_content and len(str_content.strip()) > 0:
                    if 'text=' in str_content and 'text=None' not in str_content:
                        match = re.search(r"text='([^']+)'", str_content)
                        if match:
                            result_text = match.group(1)
            except Exception:
                pass
                
    if not result_text and hasattr(response, 'text'):
        result_text = response.text
        
    # 以防部分 API 直接在一个 text block 里返回带有 <think> 标签的内容
    if result_text:
        result_text = re.sub(r'<think>.*?</think>', '', result_text, flags=re.DOTALL).strip()
        
    return result_text



def _clean_markdown_wrapper(result_text):
    """
    清理 LLM 回复格式中多余的 markdown 代码块包裹
    """
    import re
    result_text = result_text.strip()
    
    # 情况 1：被 ```markdown 包裹
    markdown_match = re.match(
        r'^```markdown\s*\n(.*?)\n```\s*$',
        result_text,
        re.DOTALL
    )
    if markdown_match:
        return markdown_match.group(1).strip()
    
    # 情况 2：被 ``` 包裹（不带 markdown 标签）
    code_block_match = re.match(
        r'^```\s*\n(.*?)\n```\s*$',
        result_text,
        re.DOTALL
    )
    if code_block_match:
        return code_block_match.group(1).strip()
    
    # 情况 3：开头有 ```markdown 或 ``` 但结尾没有（不完整的代码块）
    if result_text.startswith('```markdown'):
        result_text = result_text[len('```markdown'):].strip()
        if result_text.startswith('\n'):
            result_text = result_text[1:].strip()
    elif result_text.startswith('```'):
        result_text = result_text[3:].strip()
        if result_text.startswith('\n'):
            result_text = result_text[1:].strip()
            
    # 情况 4：结尾有 ```
    if result_text.endswith('```'):
        result_text = result_text[:-3].strip()
        
    return result_text



def _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str, is_day_ended=False):
    """
    统一格式化数据概览和应用使用情况，返回用于注入 prompt 的文本
    """
    def format_hours(hours_list):
        if not hours_list:
            return "无记录"
        return ", ".join(f"{h}点" for h in hours_list)

    def format_time_ranges(ranges_list):
        if not ranges_list:
            return "无记录"
        return ", ".join(ranges_list)

    active_ranges_str = format_time_ranges(data_summary.get('active_time_ranges', []))
    yesterday_ranges = format_time_ranges(data_summary.get('yesterday_active_time_ranges', []))
    day_before_yesterday_ranges = format_time_ranges(data_summary.get('day_before_yesterday_active_time_ranges', []))

    data_section = f"""
## 数据概览
- 日期: {target_date_str}{weekday_str}（据此推断工作日或节假日）
- 总记录数: {data_summary.get('total_records', 0)}
（注：“活动时间段”超过{ACTIVE_INTERVAL_MAX_GAP}分钟的间隔会被视为不同区间，具体以app使用时长进行判断是否活跃）
- 今日活动时间段: {active_ranges_str}
"""
    
    if yesterday_ranges != "无记录" or day_before_yesterday_ranges != "无记录":
        data_section += "- 历史辅助（仅供推断睡眠/通宵及近期规律，严禁歪曲或遗漏数字）：\n"
        if yesterday_ranges != "无记录":
            data_section += f"  - 昨天活动时间段: {yesterday_ranges}\n"
        if day_before_yesterday_ranges != "无记录":
            data_section += f"  - 前天活动时间段: {day_before_yesterday_ranges}\n"

    data_section += f"""
- 数据截止时间: {cutoff_time_str}
"""
    if not is_day_ended:
        data_section += "\n**【系统强烈提示】当前这一天还没结束！数据只同步到了上述截止时间。你的分析必须处于“正在直播”的视角，评价时要用“截至目前”，绝对不能作结案陈词（比如“今天你一共就走了xx步”、“到这就收工了”），而是要推测他接下去会干嘛。**\n"
   
    if data_summary.get('phone_app_summary'):
        data_section += f"\n## 手机应用（总计前20）\n{json.dumps(data_summary['phone_app_summary'], ensure_ascii=False)}\n"
        if data_summary.get('phone_app_by_time_range'):
            # 不过滤时间范围，保留所有应用使用记录
            filtered_time_ranges = data_summary['phone_app_by_time_range']
            
            if filtered_time_ranges:
                data_section += f"\n## 手机应用（按时间范围，[单次时长/min]或\"N次(共Xm,最长Ym)\"）\n{json.dumps(filtered_time_ranges, ensure_ascii=False)}\n"
    
    if data_summary.get('computer_app_summary'):
        data_section += f"\n## 电脑应用（总计前20）\n{json.dumps(data_summary['computer_app_summary'], ensure_ascii=False)}\n"
        if data_summary.get('computer_app_by_time_range'):
            # 不过滤时间范围，保留所有应用使用记录
            filtered_time_ranges = data_summary['computer_app_by_time_range']
            
            if filtered_time_ranges:
                data_section += f"\n## 电脑应用（按时间范围，[单次时长/min]或\"N次(共Xm,最长Ym)\"）\n{json.dumps(filtered_time_ranges, ensure_ascii=False)}\n"
    
    if data_summary.get('steps_summary'):
        data_section += f"\n## 今日总步数: {data_summary['steps_summary'].get('total', 0)}\n"
        if data_summary.get('steps_by_hour'):
            data_section += f"\n## 步数（按小时累计）\n{json.dumps(data_summary['steps_by_hour'], ensure_ascii=False)}\n"
    
    if data_summary.get('qq_messages_summary'):
        qq_summary = data_summary['qq_messages_summary']
        data_section += f"\n## QQ消息统计\n"
        data_section += f"- 总消息块数: {qq_summary.get('total_message_blocks', 0)}\n"
        data_section += f"- 群消息块数: {qq_summary.get('group_message_blocks_count', 0)}\n"
        data_section += f"- 私聊消息块数: {qq_summary.get('private_message_blocks_count', 0)}\n"
        data_section += f"- 用户消息数: {qq_summary.get('user_messages_count', 0)}\n"
        
        if qq_summary.get('group_message_count_by_group'):
            data_section += f"- 群消息分布: {json.dumps(qq_summary['group_message_count_by_group'], ensure_ascii=False)}\n"
    
    # 完整展示私聊消息中的用户发言，对LLM回复进行截断
    if data_summary.get('qq_messages'):
        qq_messages = data_summary['qq_messages']
        
        # 处理每个QQ消息记录
        for msg_record in qq_messages:
            # 检查是否是私聊消息
            if isinstance(msg_record, dict) and msg_record.get('message_type') == 'private':
                # 检查是否有message_data属性
                message_blocks = msg_record.get('message_data', [])
                if message_blocks:
                    data_section += f"\n## 私聊消息详情\n"
                    for block in message_blocks:
                        time_str = block.get('时间', '未知时间')
                        data_section += f"### {time_str}\n"
                        
                        # 处理用户发言
                        if '用户' in block:
                            user_message = block['用户']
                            data_section += f"**用户**: {user_message}\n"
                        
                        # 处理机器人回复
                        if '你的回复' in block:
                            bot_reply = block['你的回复']
                            # 对机器人回复进行截断
                            if len(bot_reply) > 100:
                                # 保留完整的第一行，然后截断
                                lines = bot_reply.split('\n')
                                if lines:
                                    truncated_reply = lines[0] + '...'
                                    data_section += f"**你的回复**: {truncated_reply}\n"
                            else:
                                data_section += f"**你的回复**: {bot_reply}\n"
                        
                        data_section += "\n"
            
    return data_section



def analyze_with_llm(aggregated_data, character_name, persona=None, ai_persona=None, system_inferred_persona=None, previous_report=None, is_incremental=False, previous_cutoff_time=None):
    """
    使用 Anthropic API 分析数据
    
    Args:
        aggregated_data: 聚合后的数据
        character_name: 角色名称
        persona: 角色人设信息（可选）- 用户的背景信息
        ai_persona: AI 人设配置（可选）- 自定义 AI 的身份、性格、语言风格
        system_inferred_persona: 系统暗中推断的真实人设档案（可选）
        previous_report: 上一份日报的 markdown 内容（用于增量更新）
        is_incremental: 是否为增量更新模式（保持风格一致性）
    
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
            'active_time_ranges': aggregated_data.get('active_time_ranges', []),
            'phone_app_summary': aggregated_data.get('phone_app_summary', {}),
            'computer_app_summary': aggregated_data.get('computer_app_summary', {}),
            'phone_app_by_time_range': aggregated_data.get('phone_app_by_time_range', {}),
            'computer_app_by_time_range': aggregated_data.get('computer_app_by_time_range', {}),
            'steps_summary': aggregated_data.get('steps_summary', {}),
            'steps_by_hour': aggregated_data.get('steps_by_hour', {}),
            'qq_messages_summary': aggregated_data.get('qq_messages_summary', {}),
            'qq_messages': aggregated_data.get('qq_messages', []),
            'last_record_time': aggregated_data.get('last_record_time'),
            'data_cutoff_time': aggregated_data.get('data_cutoff_time'),
            'yesterday_active_time_ranges': aggregated_data.get('yesterday_active_time_ranges', []),
            'day_before_yesterday_active_time_ranges': aggregated_data.get('day_before_yesterday_active_time_ranges', [])
        }
        
        ai_persona = ai_persona or {}
        
        core_identity = ai_persona.get('core_identity', '')
        personality_traits = ai_persona.get('personality_traits', '')
        language_style = ai_persona.get('language_style', '')
        
        has_custom_ai_persona = bool(core_identity or personality_traits or language_style)
        
        if has_custom_ai_persona:
            ai_identity_parts = []
            if core_identity:
                ai_identity_parts.append(core_identity)
            if personality_traits:
                ai_identity_parts.append(personality_traits)
            
            ai_identity_desc = "\n".join(ai_identity_parts)
            
            language_style_section = ""
            if language_style:
                language_style_section = f"\n## 语言风格\n{language_style}\n"
            
            system_prompt = f"{ai_identity_desc}\n{language_style_section}\n{CUSTOM_SYSTEM_RULES_APPENDIX}"
        else:
            system_prompt = DEFAULT_SYSTEM_PROMPT

        cutoff_time_str = data_summary.get('data_cutoff_time', '未知')
        
        target_date_str = data_summary.get('date', '')
        weekday_str = ""
        if target_date_str:
            try:
                target_date_obj = timezone.datetime.fromisoformat(target_date_str).date()
                weekday_map = {0: '星期一', 1: '星期二', 2: '星期三', 3: '星期四', 4: '星期五', 5: '星期六', 6: '星期日'}
                weekday_str = f" ({weekday_map[target_date_obj.weekday()]})"
            except Exception:
                pass
                
        is_day_ended = not is_incremental
        if cutoff_time_str and cutoff_time_str != '未知' and target_date_str:
            try:
                cutoff_dt = timezone.datetime.fromisoformat(cutoff_time_str)
                local_cutoff_dt = timezone.localtime(cutoff_dt)
                if local_cutoff_dt.date() > target_date_obj:
                    is_day_ended = True
                else:
                    is_day_ended = False
            except Exception:
                pass
        
        if not is_day_ended and previous_report and previous_report.strip():
            # 清理上一份日报末尾由于代码自动拼接的数据截止时间尾巴，避免误导大模型或产生双重尾巴
            import re
            clean_previous_report = re.sub(r'\n+---\n+\*数据截止至：.*?\*\s*$', '', previous_report.strip())
            
            data_section = _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str, is_day_ended=is_day_ended)
            
            # 格式化上一次的截止时间
            prev_time_str = "之前"
            if previous_cutoff_time:
                prev_time_str = timezone.localtime(previous_cutoff_time).strftime('%Y-%m-%d %H:%M')
            curr_time_str = cutoff_time_str
            if curr_time_str and curr_time_str != '未知':
                try:
                    dt = timezone.datetime.fromisoformat(curr_time_str)
                    curr_time_str = dt.strftime('%Y-%m-%d %H:%M')
                except Exception:
                    pass
            
            user_prompt = INCREMENTAL_UPDATE_PROMPT.format(
                character_name=character_name,
                prev_time_str=prev_time_str,
                curr_time_str=curr_time_str,
                clean_previous_report=clean_previous_report,
                data_section=data_section
            )
        elif is_day_ended and previous_report and previous_report.strip():
            # 最终总结阶段：整合所有带有中间过程标题的旧日报
            import re
            clean_previous_report = re.sub(r'\n+---\n+\*数据截止至：.*?\*\s*$', '', previous_report.strip())
            
            data_section = _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str, is_day_ended=is_day_ended)
            
            user_prompt = FINAL_SUMMARY_PROMPT.format(
                clean_previous_report=clean_previous_report,
                data_section=data_section
            )
            if persona and persona.strip():
                user_prompt += f"\n## 用户自述角色背景\n{persona.strip()}\n"
                
            if system_inferred_persona and system_inferred_persona.strip():
                user_prompt += f"\n## 你观察得出的真实侧写档案\n{system_inferred_persona.strip()}"
                
        else:
            user_prompt = f"请对用户 {character_name} 在 {data_summary.get('date')} 的活动进行分析。\n"
            
            if persona and persona.strip():
                user_prompt += f"\n## 用户自述角色背景\n{persona.strip()}\n"
                
            if system_inferred_persona and system_inferred_persona.strip():
                user_prompt += f"\n## 你观察得出的真实侧写档案\n{system_inferred_persona.strip()}"
            elif persona and persona.strip():
                user_prompt += "\n请结合上述自述背景进行分析，使分析更贴合角色。\n"

            data_section = _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str, is_day_ended=is_day_ended)
            user_prompt += data_section
                
        # 决定是否需要追加格式规范：
        # 1. 增量更新模式 (未完结且有旧日报) -> 不需要，让它继承旧日报的格式
        # 2. 从0生成 (无论是否完结) -> 需要
        # 3. 最终结案 (完结且有旧日报) -> 需要 (重构成正式结构)
        is_incremental_update = (not is_day_ended) and previous_report and previous_report.strip()
        
        if not is_incremental_update:
            qq_summary = data_summary.get('qq_messages_summary', {})
            has_qq = qq_summary.get('total_message_blocks', 0) > 0
            
            if has_custom_ai_persona:
                qq_sec = CUSTOM_QQ_FORMAT_SECTION if has_qq else ""
                user_prompt += f"\n{CUSTOM_FORMAT_INSTRUCTIONS.format(qq_format_section=qq_sec)}"
            else:
                qq_sec = DEFAULT_QQ_FORMAT_SECTION if has_qq else ""
                user_prompt += f"\n{DEFAULT_FORMAT_INSTRUCTIONS.format(qq_format_section=qq_sec)}"

        print("\n" + "="*20 + " LLM Analysis Prompt Start " + "="*20)
        print(f"System Prompt:\n{system_prompt}")
        print(f"\nUser Prompt:\n{user_prompt}")
        print("="*20 + " LLM Analysis Prompt End " + "="*20 + "\n")
        
        logger.info("=== LLM Analysis Prompt Start ===")
        logger.info(f"System Prompt:\n{system_prompt}")
        logger.info(f"User Prompt:\n{user_prompt}")
        logger.info("=== LLM Analysis Prompt End ===")

        response = client.messages.create(
            model=model,
            system=system_prompt,
            temperature=0.8,
            max_tokens=8192,
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
            
        if getattr(response, 'stop_reason', None) == 'max_tokens':
            logger.error(f"LLM response truncated due to max_tokens! Response: {response}")
            return {
                'markdown': '## 分析失败\n\nAI 回复由于长度限制被截断了（可能是思考过程过长）。',
                'error': 'Response truncated due to max_tokens limit'
            }
        
        result_text = extract_text_from_anthropic_response(response)
        
        if result_text is None or not isinstance(result_text, str) or len(result_text.strip()) == 0:
            logger.error(f"Failed to extract text from any block. Response: {response.content}")
            return {
                'markdown': '## 分析失败\n\nAI 返回了无效的响应格式。',
                'error': 'Failed to extract text from response'
            }
        
        logger.info(f"Successfully extracted text, length: {len(result_text)}")
        
        # 清理 LLM 回复格式
        result_text = _clean_markdown_wrapper(result_text)
        logger.info(f"Cleaned text length: {len(result_text)}")
        
        # 自动在结尾拼接数据截止时间，提升展示效果
        cutoff_time_str = data_summary.get('data_cutoff_time')
        if cutoff_time_str:
            try:
                dt = timezone.datetime.fromisoformat(cutoff_time_str)
                # 由于已经是 localtime，可以直接提取出时分等信息
                formatted_time = dt.strftime('%Y-%m-%d %H:%M')
                result_text += f"\n\n---\n*数据截止至：{formatted_time}*"
            except Exception:
                result_text += f"\n\n---\n*数据截止至：{cutoff_time_str}*"
        
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


