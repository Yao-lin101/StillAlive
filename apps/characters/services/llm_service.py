import json
import logging
from django.utils import timezone
from django.conf import settings
from .prompts import (
    DEFAULT_SYSTEM_PROMPT, CUSTOM_SYSTEM_RULES_APPENDIX,
    INCREMENTAL_UPDATE_PROMPT, FINAL_SUMMARY_PROMPT,
    CUSTOM_FORMAT_INSTRUCTIONS, DEFAULT_FORMAT_INSTRUCTIONS
)

logger = logging.getLogger(__name__)

def extract_text_from_anthropic_response(response):
    """
    安全地从 Anthropic API 的 response 中提取文本，兼容带有 thinking block 的模型。
    """
    result_text = None
    thinking_content = None
    
    if hasattr(response, 'content'):
        for i, block in enumerate(response.content):
            block_type = getattr(block, 'type', 'unknown')
            
            if block_type == 'text':
                if hasattr(block, 'text') and block.text is not None:
                    result_text = block.text
                    break
            
            if block_type == 'thinking':
                if hasattr(block, 'thinking') and block.thinking is not None:
                    thinking_content = block.thinking
                    
        if result_text is None:
            for i, block in enumerate(response.content):
                if hasattr(block, 'text') and block.text is not None:
                    result_text = block.text
                    break
        
        if result_text is None:
            try:
                str_content = str(response.content[-1])
                if str_content and len(str_content.strip()) > 0:
                    if 'text=' in str_content and 'text=None' not in str_content:
                        import re
                        match = re.search(r"text='([^']+)'", str_content)
                        if match:
                            result_text = match.group(1)
            except Exception:
                pass
                
    if not result_text and hasattr(response, 'text'):
        result_text = response.text
        
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



def _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str, is_incremental=False):
    """
    统一格式化数据概览和应用使用情况，返回用于注入 prompt 的文本
    """
    data_section = f"""
## 数据概览
- 日期: {target_date_str}{weekday_str}（据此推断工作日或节假日）
- 总记录数: {data_summary.get('total_records', 0)}
- 活动小时: {data_summary.get('active_hours', [])}
- 首次活动时间: {data_summary.get('first_activity_hour', '未知')} 点
- 最后活动时间: {data_summary.get('last_activity_hour', '未知')} 点
- 数据截止时间: {cutoff_time_str}
"""
    is_day_ended = not is_incremental
    if cutoff_time_str and cutoff_time_str != '未知' and target_date_str:
        try:
            cutoff_dt = timezone.datetime.fromisoformat(cutoff_time_str)
            target_date_obj = timezone.datetime.fromisoformat(target_date_str).date()
            local_cutoff_dt = timezone.localtime(cutoff_dt)
            # 如果数据截止时间已经是目标日期的第二天或更晚，说明这一天已经彻底结束
            if local_cutoff_dt.date() > target_date_obj:
                is_day_ended = True
            else:
                is_day_ended = False
        except Exception:
            pass

    if not is_day_ended:
        data_section += "\n**【系统强烈提示】当前这一天还没结束！数据只同步到了上述截止时间。你的分析必须处于“正在直播”的视角，评价时要用“截至目前”，绝对不能作结案陈词（比如“今天你一共就走了xx步”、“到这就收工了”），而是要推测他接下去会干嘛。**\n"
    else:
        data_section += "\n**【系统强烈提示】今天已经彻底结束！这是全天的最终结案数据。请进行盖棺定论的总结，绝对不要使用“截至目前”、“进行中”、“还在持续”、“推测接下去会干嘛”等未完结的直播语气！**\n"
   
    if data_summary.get('phone_app_summary'):
        data_section += f"\n## 手机应用（总计前20）\n{json.dumps(data_summary['phone_app_summary'], ensure_ascii=False)}\n"
        data_section += f"\n## 手机应用（按小时）\n{json.dumps(data_summary.get('phone_app_by_hour', {}), ensure_ascii=False)}\n"
    
    if data_summary.get('computer_app_summary'):
        data_section += f"\n## 电脑应用（总计前20）\n{json.dumps(data_summary['computer_app_summary'], ensure_ascii=False)}\n"
        data_section += f"\n## 电脑应用（按小时）\n{json.dumps(data_summary.get('computer_app_by_hour', {}), ensure_ascii=False)}\n"
    
    if data_summary.get('steps_summary'):
        data_section += f"\n## 今日总步数: {data_summary['steps_summary'].get('total', 0)}\n"
        if data_summary.get('steps_by_hour'):
            data_section += f"\n## 步数（按小时累计）\n{json.dumps(data_summary['steps_by_hour'], ensure_ascii=False)}\n"
            
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
        
        if is_incremental and previous_report and previous_report.strip():
            # 清理上一份日报末尾由于代码自动拼接的数据截止时间尾巴，避免误导大模型或产生双重尾巴
            import re
            clean_previous_report = re.sub(r'\n+---\n+\*数据截止至：.*?\*\s*$', '', previous_report.strip())
            
            data_section = _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str, is_incremental=is_incremental)
            
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
        elif not is_incremental and previous_report and previous_report.strip():
            # 最终总结阶段：整合所有带有中间过程标题的旧日报
            import re
            clean_previous_report = re.sub(r'\n+---\n+\*数据截止至：.*?\*\s*$', '', previous_report.strip())
            
            data_section = _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str, is_incremental=is_incremental)
            
            user_prompt = FINAL_SUMMARY_PROMPT.format(
                clean_previous_report=clean_previous_report,
                data_section=data_section
            )
            if persona and persona.strip():
                user_prompt += f"\n## 用户自述角色背景\n{persona.strip()}\n"
                
            if system_inferred_persona and system_inferred_persona.strip():
                user_prompt += f"\n## 系统长期观察得出的真实侧写档案\n{system_inferred_persona.strip()}"
                
        else:
            user_prompt = f"请对用户 {character_name} 在 {data_summary.get('date')} 的活动进行分析。\n"
            
            if persona and persona.strip():
                user_prompt += f"\n## 用户自述角色背景\n{persona.strip()}\n"
                
            if system_inferred_persona and system_inferred_persona.strip():
                user_prompt += f"\n## 系统长期观察得出的真实侧写档案\n{system_inferred_persona.strip()}"
            elif persona and persona.strip():
                user_prompt += "\n请结合上述自述背景进行分析，使分析更贴合角色。\n"

            data_section = _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str, is_incremental=is_incremental)
            user_prompt += data_section
                
        if not is_incremental:
            if has_custom_ai_persona:
                user_prompt += f"\n{CUSTOM_FORMAT_INSTRUCTIONS}"
            else:
                user_prompt += f"\n{DEFAULT_FORMAT_INSTRUCTIONS}"

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


