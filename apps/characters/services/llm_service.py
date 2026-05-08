import json
import logging
from django.utils import timezone
from django.conf import settings
from .prompts import (
    DEFAULT_SYSTEM_PROMPT, COMMON_ANALYSIS_RULES,
    INCREMENTAL_UPDATE_PROMPT, FINAL_SUMMARY_PROMPT,
    CUSTOM_FORMAT_INSTRUCTIONS, DEFAULT_FORMAT_INSTRUCTIONS,
    CUSTOM_QQ_FORMAT_SECTION, DEFAULT_QQ_FORMAT_SECTION,
    META_INSTRUCTION_EXTRACTION_PROMPT, STRUCTURED_SYSTEM_PROMPT,
    INITIAL_REPORT_PROMPT
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



def _get_data_keys(data_summary):
    """
    从数据摘要中提取所有可能的敏感键值（应用名、标题、话题等）
    """
    keys = set()
    
    # 1. 手机应用名 (从 summary 提取键名)
    if 'phone_app_summary' in data_summary and isinstance(data_summary['phone_app_summary'], dict):
        keys.update(data_summary['phone_app_summary'].keys())
    
    # 2. 电脑应用名 (从 summary 提取键名)
    if 'computer_app_summary' in data_summary and isinstance(data_summary['computer_app_summary'], dict):
        keys.update(data_summary['computer_app_summary'].keys())
        
    # 3. 详细时间段中的应用和标题 (电脑端的 titles)
    # 手机端
    if 'phone_app_by_time_range' in data_summary:
        for range_apps in data_summary['phone_app_by_time_range'].values():
            if isinstance(range_apps, dict):
                keys.update(range_apps.keys())
    
    # 电脑端 (包含 app 名和可能的标题)
    if 'computer_app_by_time_range' in data_summary:
        for range_apps in data_summary['computer_app_by_time_range'].values():
            if isinstance(range_apps, dict):
                keys.update(range_apps.keys())
    
    # 4. 聊天记录 (目前认为话题本身已足够模糊，不需要作为脱敏 Key，故移除)
    # if 'qq_messages' in data_summary:
    #     ...
                
    return [k for k in keys if k and k.strip()]


def _extract_meta_instructions(client, model, private_blocks, data_keys=[]):
    """
    第一阶段：从私聊记录中提取元指令和脱敏需求
    """
    if not private_blocks:
        return {"instructions": [], "redactions": {}, "has_any": False}

    # 格式化私聊记录供提取使用
    chat_content = ""
    for block in private_blocks:
        # 兼容原始对话格式
        if '用户' in block:
            chat_content += f"用户: {block['用户']}\n"
        if '你的回复' in block:
            chat_content += f"你: {block['你的回复']}\n"
        
        # 兼容总结后的对话格式
        if '话题' in block:
            chat_content += f"话题: {block['话题']}\n"
        if '总结' in block:
            chat_content += f"总结: {block['总结']}\n"
            
        chat_content += "---\n"

    try:
        # 改用换行列表形式，避免标题内逗号导致歧义
        data_keys_str = "\n".join([f"- {k}" for k in data_keys])
        prompt = META_INSTRUCTION_EXTRACTION_PROMPT.format(
            private_chat_content=chat_content,
            data_keys=data_keys_str
        )
        
        # 打印提取阶段的日志
        print("\n" + "="*30 + " [STAGE 1: AUDIT & REDACTION] " + "="*30)
        print(f"PROMPT SENT TO LLM:\n{prompt}")

        response = client.messages.create(
            model=model,
            max_tokens=4000,  # 增加 token 限制以防脱敏表太长被截断
            temperature=0,
            messages=[{"role": "user", "content": prompt}]
        )
        
        raw_result = extract_text_from_anthropic_response(response)
        print(f"RAW LLM RESPONSE (Length: {len(raw_result)}):\n{raw_result}")
        
        # 尝试解析 JSON
        import json
        import re
        
        try:
            # 预处理：修复颜文字或标题中未转义的反斜杠 (例如 \ ( ) -> \\ ( ) )
            # 这是一个简单的启发式修复：将所有反斜杠替换为双反斜杠，但要避开已经是转义的内容
            # 为简单起见，我们直接处理最常见的干扰项
            processed_raw = raw_result.replace('\\', '\\\\')
            # 但是上面的操作会把本就正确的 \" 变成 \\\"，需要修正回来
            processed_raw = processed_raw.replace('\\\\"', '\\"')

            # 处理可能存在的 Markdown 代码块包裹
            json_match = re.search(r'(\{.*\})', processed_raw, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group(1))
            else:
                result = json.loads(processed_raw.strip())
                
            if not isinstance(result, dict):
                result = {"instructions": [], "redactions": {}, "has_any": False}
                
            # 打印审计结果摘要
            instr_count = len(result.get('instructions') or [])
            redact_count = len(result.get('redactions') or {})
            print(f"AUDIT RESULT: Instructions({instr_count}), Redactions({redact_count})")
            print("="*100 + "\n")
            
            return result
        except Exception as json_err:
            print(f"JSON Parsing failed: {json_err}")
            # 如果截断了，尝试闭合大括号进行“抢救性解析” (可选，但通常返回空更安全)
            return {"instructions": [], "redactions": {}, "has_any": False}
    except Exception as e:
        logger.error(f"Failed to extract meta-instructions: {e}")
        return {"instructions": [], "redactions": {}, "has_any": False}



def _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str, is_day_ended=False, include_system_prompt=True):
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

    global_ranges = data_summary.get('global_active_time_ranges', [])
    
    data_section = f"""
## 数据概览
- 日期: {target_date_str}{weekday_str}（据此推断工作日或节假日）
- 总记录数: {data_summary.get('total_records', 0)}
"""
    
    if global_ranges:
        ranges_str = "\n".join([f"- {r}" for r in global_ranges])
        data_section += f"""
## 近期连续活跃周期
（注：已自动合并跨天活动，相差{ACTIVE_INTERVAL_MAX_GAP}分钟以内的活动将被连接。两个周期之间的空白时间代表设备脱机、人在休息或睡眠。请重点关注这些“未列出的空白时间”来推断脱机长短。）
{ranges_str}
"""

    data_section += f"""
- 数据截止时间: {cutoff_time_str}
"""
    # 注意：这里的系统提示词逻辑稍后将在 analyze_with_llm 中重构，目前保留基础数据
   
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
    
    # 处理并展示QQ聊天记录和群聊总结
    if data_summary.get('qq_messages'):
        qq_messages = data_summary['qq_messages']
        
        private_blocks = []
        group_blocks = []
        
        for msg_record in qq_messages:
            if isinstance(msg_record, dict):
                msg_type = msg_record.get('message_type')
                message_data = msg_record.get('message_data', [])
                if msg_type == 'private' and message_data:
                    private_blocks.extend(message_data)
                elif msg_type == 'group' and message_data:
                    group_blocks.extend(message_data)
                    
        if private_blocks:
            data_section += f"\n## 这是用户今天和你的私人聊天内容：\n"
            for block in private_blocks:
                time_str = block.get('时间', '未知时间')
                data_section += f"[{time_str}]\n"
                
                # 处理话题和总结
                if '话题' in block:
                    data_section += f"话题: {block['话题']}\n"
                if '总结' in block:
                    data_section += f"总结: {block['总结']}\n"
                
                # 处理用户发言
                if '用户' in block:
                    user_message = block['用户']
                    data_section += f"用户: {user_message}\n"
                
                # 处理机器人回复
                if '你的回复' in block:
                    bot_reply = block['你的回复']
                    # 对机器人回复进行截断
                    if len(bot_reply) > 100:
                        # 保留完整的第一行，然后截断
                        lines = bot_reply.split('\n')
                        if lines:
                            truncated_reply = lines[0] + '...'
                            data_section += f"你: {truncated_reply}\n"
                    else:
                        data_section += f"你: {bot_reply}\n"
                
                data_section += "\n"
                
        if group_blocks:
            data_section += f"\n## QQ群聊内容总结\n"
            
            # 按群名称聚合
            grouped_chats = {}
            for block in group_blocks:
                group_name = block.get('群名称', '未知群聊')
                if group_name not in grouped_chats:
                    grouped_chats[group_name] = {
                        'bot_nickname': block.get('你在本群昵称', '未知'),
                        'user_nickname': block.get('用户在本群昵称', '未知'),
                        'topics': []
                    }
                grouped_chats[group_name]['topics'].append({
                    'time': block.get('时间', '未知'),
                    'summary': block.get('话题总结', '')
                })
                
            for group_name, info in grouped_chats.items():
                data_section += f"### 【{group_name}】\n"
                data_section += f"- 你的群昵称: {info['bot_nickname']}\n"
                data_section += f"- 用户的群昵称: {info['user_nickname']}\n\n"
                for t in info['topics']:
                    data_section += f"#### [{t['time']}]\n{t['summary']}\n\n"
            
    return data_section



def analyze_with_llm(
    aggregated_data,
    character_name,
    persona=None,
    ai_persona=None,
    system_inferred_persona=None,
    previous_report=None,
    is_incremental=False,
    previous_cutoff_time=None,
    long_term_memory_context=None,
    include_important_events=True,
):
    """
    使用 Anthropic API 分析数据 (兼容 MiniMax)
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
            'day_before_yesterday_active_time_ranges': aggregated_data.get('day_before_yesterday_active_time_ranges', []),
            'global_active_time_ranges': aggregated_data.get('global_active_time_ranges', [])
        }
        
        # 1. 指令提取与脱敏审计阶段
        private_blocks = []
        for msg_record in data_summary.get('qq_messages', []):
            if isinstance(msg_record, dict) and msg_record.get('message_type') == 'private':
                private_blocks.extend(msg_record.get('message_data', []))
        
        data_keys = _get_data_keys(data_summary)
        audit_result = _extract_meta_instructions(client, model, private_blocks, data_keys)
        
        meta_instructions = "\n".join(audit_result.get('instructions', []))
        redaction_map = audit_result.get('redactions', {})
        has_meta = audit_result.get('has_any', False)
        
        # 2. 构建 System Prompt 
        ai_persona = ai_persona or {}
        core_identity = ai_persona.get('core_identity', '')
        personality_traits = ai_persona.get('personality_traits', '')
        language_style = ai_persona.get('language_style', '')
        
        if core_identity or personality_traits:
            ai_identity_desc = f"{core_identity}\n{personality_traits}"
        else:
            ai_identity_desc = DEFAULT_SYSTEM_PROMPT

        if language_style:
            ai_identity_desc += f"\n## 语言风格\n{language_style}\n"
        
        cutoff_time_str = data_summary.get('data_cutoff_time', '未知')
        target_date_str = data_summary.get('date', '')
        
        is_day_ended = not is_incremental
        if cutoff_time_str and cutoff_time_str != '未知' and target_date_str:
            try:
                cutoff_dt = timezone.datetime.fromisoformat(cutoff_time_str)
                target_date_obj = timezone.datetime.fromisoformat(target_date_str).date()
                local_cutoff_dt = timezone.localtime(cutoff_dt)
                if local_cutoff_dt.date() > target_date_obj:
                    is_day_ended = True
                else:
                    is_day_ended = False
            except Exception:
                pass
        
        report_mode = "全天结案总结" if is_day_ended else "日间增量更新"
        mode_hint = "请对全天进行复盘，给出最终定性结论。" if is_day_ended else "请以'截至目前'的视角进行锐评，推测后续活动。"
        
        # 决定输出格式要求
        qq_summary = data_summary.get('qq_messages_summary', {})
        has_qq = qq_summary.get('total_message_blocks', 0) > 0
        has_custom_ai_persona = bool(core_identity or personality_traits or language_style)
        
        if has_custom_ai_persona:
            qq_sec = CUSTOM_QQ_FORMAT_SECTION if has_qq else ""
            format_instructions = CUSTOM_FORMAT_INSTRUCTIONS.format(qq_format_section=qq_sec)
        else:
            qq_sec = DEFAULT_QQ_FORMAT_SECTION if has_qq else ""
            format_instructions = DEFAULT_FORMAT_INSTRUCTIONS.format(qq_format_section=qq_sec)

        # 准备用户画像信息（包含长期记忆）
        user_persona_full = persona.strip() if persona else "无"
        if include_important_events and long_term_memory_context and long_term_memory_context.strip():
            user_persona_full += f"\n\n### 历史重要记忆\n{long_term_memory_context.strip()}"

        # 动态构建特殊指令板块
        meta_instructions_section = ""
        if meta_instructions and meta_instructions.strip():
            meta_instructions_section = f"""# 本次任务特殊约束 (Meta-Instructions)
<special_constraints>
{meta_instructions}
</special_constraints>
**保密执行准则**：由于存在上述特殊约定，你在生成日报时，可以根据你的人设风格将这些变动或禁令比作“隐藏任务”、“秘密约定”或“私下沟通”等方式略带提及（以增加互动感）。但你必须严格遵守约束，绝对禁止在报告中泄露任何被要求保密的具体细节内容。"""

        system_prompt = STRUCTURED_SYSTEM_PROMPT.format(
            ai_identity_desc=ai_identity_desc,
            character_name=character_name,
            user_persona=user_persona_full,
            system_inferred_persona=system_inferred_persona.strip() if system_inferred_persona else "尚无深度侧写",
            common_rules=COMMON_ANALYSIS_RULES,
            report_mode=report_mode,
            mode_hint=mode_hint,
            cutoff_time=cutoff_time_str,
            meta_instructions_section=meta_instructions_section,
            format_instructions=format_instructions
        )

        # 3. 构建 User Prompt (Data Only)
        data_section = _build_data_section(data_summary, target_date_str, "", cutoff_time_str, is_day_ended=is_day_ended, include_system_prompt=False)
        
        if not is_day_ended and previous_report and previous_report.strip():
            import re
            clean_previous_report = re.sub(r'\n+---\n+\*数据截止至：.*?\*\s*$', '', previous_report.strip())
            prev_time_str = "之前"
            if previous_cutoff_time:
                prev_time_str = timezone.localtime(previous_cutoff_time).strftime('%Y-%m-%d %H:%M')
            
            user_prompt = INCREMENTAL_UPDATE_PROMPT_V2.format(
                prev_time_str=prev_time_str,
                curr_time_str=cutoff_time_str,
                clean_previous_report=clean_previous_report,
                data_section=data_section
            )
        elif is_day_ended and previous_report and previous_report.strip():
            import re
            clean_previous_report = re.sub(r'\n+---\n+\*数据截止至：.*?\*\s*$', '', previous_report.strip())
            user_prompt = FINAL_SUMMARY_PROMPT_V2.format(
                clean_previous_report=clean_previous_report,
                data_section=data_section
            )
        else:
            user_prompt = INITIAL_REPORT_PROMPT.format(
                character_name=character_name,
                data_section=data_section
            )

        # 4. 执行数据脱敏 (Data Redaction)
        # 仅针对即将发送给 LLM 的 user_prompt 进行全局替换
        if redaction_map:
            print(f"Applying redactions: {len(redaction_map)} items...")
            for original, replacement in redaction_map.items():
                if original and original.strip():
                    user_prompt = user_prompt.replace(original, replacement)

        # 5. 动态追加末尾强化提醒 (针对特殊约束内容复述)
        if meta_instructions and meta_instructions.strip():
            user_prompt += f"\n\n**再次提醒**：请务必检查并严格遵守以下【本次任务特殊约束】，确保输出内容完全符合用户的最新指示：\n{meta_instructions}"

        print("\n" + "="*20 + " LLM Analysis Prompt Start " + "="*20)
        print(f"System Prompt:\n{system_prompt}")
        print(f"\nUser Prompt:\n{user_prompt}")
        print("="*20 + " LLM Analysis Prompt End " + "="*20 + "\n")
        
        logger.debug("=== LLM Analysis Prompt Start ===")
        logger.debug(f"System Prompt:\n{system_prompt}")
        logger.debug(f"User Prompt:\n{user_prompt}")
        logger.debug("=== LLM Analysis Prompt End ===")

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
            return {'markdown': '## 分析失败\n\nAI 返回了空响应。', 'error': 'LLM response is empty'}
            
        result_text = extract_text_from_anthropic_response(response)
        result_text = _clean_markdown_wrapper(result_text)
        
        # 拼接截止时间
        if cutoff_time_str:
            try:
                dt = timezone.datetime.fromisoformat(cutoff_time_str)
                formatted_time = dt.strftime('%Y-%m-%d %H:%M')
                result_text += f"\n\n---\n*数据截止至：{formatted_time}*"
            except Exception:
                result_text += f"\n\n---\n*数据截止至：{cutoff_time_str}*"
        
        return {'markdown': result_text}
        
    except Exception as e:
        logger.error(f"LLM analysis failed: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return {'markdown': f'## 分析失败\n\n分析过程中发生错误：{str(e)}', 'error': str(e)}
