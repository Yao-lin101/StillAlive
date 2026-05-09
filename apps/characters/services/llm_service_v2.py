import json
import logging
import re
from django.utils import timezone
from django.conf import settings
from .prompts import STRUCTURED_SYSTEM_PROMPT, COMMON_ANALYSIS_RULES
from . import prompts_v2 as pv2
from .llm_service import (
    extract_text_from_anthropic_response, 
    _clean_markdown_wrapper, 
    _get_data_keys, 
    _extract_meta_instructions,
    _build_data_section
)

logger = logging.getLogger(__name__)

def _safe_json_loads(text):
    """尝试解析 LLM 返回的 JSON，处理可能的垃圾字符"""
    if not text:
        return None
    try:
        # 尝试寻找第一个 { 和最后一个 }
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            json_str = text[start:end+1]
            return json.loads(json_str)
        return json.loads(text)
    except Exception as e:
        logger.error(f"JSON parse error: {str(e)}\nRaw text: {text}")
        return None

def analyze_module_structured(
    module_key,
    data_summary,
    character_name,
    persona_info,
    previous_module_data=None,
    meta_constraints=None,
    long_term_memory_context=None,
    other_modules_context=None,
    compact_mode=False
):
    """
    单模块结构化分析核心函数
    """
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    model = getattr(settings, 'ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')
    base_url = getattr(settings, 'ANTHROPIC_BASE_URL', None)

    if not api_key:
        return {"error": "API Key not configured"}

    import anthropic
    client_kwargs = {'api_key': api_key}
    if base_url: client_kwargs['base_url'] = base_url
    client = anthropic.Anthropic(**client_kwargs)

    # 1. 准备 System Prompt
    ai_persona = persona_info.get('ai_persona') or {}
    core_identity = ai_persona.get('core_identity') or "你是一位精准的数据分析专家。"
    personality_traits = ai_persona.get('personality_traits') or "理性、严谨、客观。"
    language_style = ai_persona.get('language_style') or "口语化，表达自然、流畅，保持你的角色口吻。"
    
    # 注入该模块特有的 Format Instructions
    format_instr_map = {
        'title_summary': pv2.TITLE_SUMMARY_FORMAT_INSTRUCTIONS,
        'schedule': pv2.SCHEDULE_FORMAT_INSTRUCTIONS,
        'activity': pv2.ACTIVITY_FORMAT_INSTRUCTIONS,
        'findings': pv2.FINDINGS_FORMAT_INSTRUCTIONS,
        'chat': pv2.CHAT_FORMAT_INSTRUCTIONS,
    }
    format_instructions = format_instr_map.get(module_key, "")

    # 构建动态分析规则 (使用 V2 专有规则)
    common_rules = pv2.BASE_V2_ANALYSIS_RULES
    if module_key == 'schedule':
        common_rules += pv2.STEPS_V2_TRAP_RULE
    elif module_key == 'activity':
        common_rules += pv2.APP_STAY_V2_TRAP_RULE
    elif module_key == 'chat':
        common_rules += pv2.CHAT_V2_TRAP_RULE
    elif module_key in ['findings', 'title_summary']:
        # 这些模块可能涉及所有数据，所以都加上
        common_rules += pv2.STEPS_V2_TRAP_RULE + pv2.APP_STAY_V2_TRAP_RULE + pv2.CHAT_V2_TRAP_RULE

    system_prompt = pv2.V2_STRUCTURED_SYSTEM_PROMPT.format(
        core_identity=core_identity,
        personality_traits=personality_traits,
        language_style=language_style,
        character_name=character_name,
        user_persona=persona_info.get('persona', '无'),
        system_inferred_persona=persona_info.get('system_inferred_persona', '无'),
        common_rules=common_rules,
        report_mode="模块化增量更新",
        mode_hint="请按照指定的 JSON 格式输出，保持角色沉浸。",
        cutoff_time=data_summary.get('data_cutoff_time', '未知'),
        meta_instructions_section=f"\n# 特殊约束\n{meta_constraints}" if meta_constraints else "",
        format_instructions=format_instructions
    )

    # 2. 准备 User Prompt (根据模块不同，构建不同的数据上下文)
    # 对于作息分析模块，剔除应用数据块，只保留步数和活跃周期
    exclude_apps = (module_key == 'schedule')
    # 对于活动画像模块，剔除步数和活跃周期数据，只保留应用数据
    exclude_steps_and_ranges = (module_key == 'activity')
    
    data_section = _build_data_section(
        data_summary, 
        data_summary.get('date'), 
        "", 
        data_summary.get('data_cutoff_time'), 
        is_day_ended=False, 
        include_system_prompt=False,
        exclude_apps=exclude_apps,
        exclude_steps_and_ranges=exclude_steps_and_ranges,
        compact_mode=compact_mode
    )

    existing_section = ""
    if module_key == 'schedule':
        # 提取已锁定的 slots
        prev_data = previous_module_data or {}
        locked_slots = [s for s in prev_data.get('slots', []) if s.get('locked')]
        # 计算哪些是新时段（简单逻辑：当前数据中有但 locked_slots 中没有的）
        # 这里简单化处理，由 LLM 判断
        existing_section = pv2.SCHEDULE_EXISTING_SLOTS_TEMPLATE.format(
            locked_slots_json=json.dumps(locked_slots, ensure_ascii=False, indent=2),
            new_slots_list="请基于快照中的 active_time_ranges 识别新时段"
        )
        user_prompt = pv2.SCHEDULE_USER_PROMPT.format(
            character_name=character_name,
            data_section=data_section,
            existing_slots_section=existing_section
        )
    elif module_key == 'activity':
        prev_data = previous_module_data or {}
        locked_slots = [s for s in prev_data.get('slots', []) if s.get('locked')]
        existing_section = pv2.SCHEDULE_EXISTING_SLOTS_TEMPLATE.format( # 复用模板
            locked_slots_json=json.dumps(locked_slots, ensure_ascii=False, indent=2),
            new_slots_list="请基于快照中的 App 使用时段识别新时段"
        )
        user_prompt = pv2.ACTIVITY_USER_PROMPT.format(
            character_name=character_name,
            data_section=data_section,
            existing_slots_section=existing_section
        )
    elif module_key == 'findings':
        prev_data = previous_module_data or {}
        locked_slots = [s for s in prev_data.get('slots', []) if s.get('locked')]
        existing_section = pv2.SCHEDULE_EXISTING_SLOTS_TEMPLATE.format(
            locked_slots_json=json.dumps(locked_slots, ensure_ascii=False, indent=2),
            new_slots_list="请挖掘今日全量数据中的新发现或有趣时段"
        )
        user_prompt = pv2.FINDINGS_USER_PROMPT.format(
            character_name=character_name,
            data_section=data_section,
            existing_slots_section=existing_section,
            other_modules_section=f"\n# 已有模块分析结论（供参考）\n{other_modules_context}" if other_modules_context else ""
        )
    elif module_key == 'chat':
        # 这里比较特殊，需要过滤出新的聊天记录
        # 简单起见，目前先传全量，让 LLM 根据已有的 items 进行去重
        prev_data = previous_module_data or {}
        locked_items = prev_data.get('items', [])
        existing_section = pv2.CHAT_EXISTING_ITEMS_TEMPLATE.format(
            locked_items_json=json.dumps(locked_items, ensure_ascii=False, indent=2),
            new_topics_list="请分析快照中新增的消息块"
        )
        # 提取聊天部分的数据（支持嵌套的消息块结构）
        chat_section = ""
        qq_messages = data_summary.get('qq_messages', [])
        
        private_list = []
        group_list = []
        for block in qq_messages:
            m_type = block.get('message_type')
            m_data = block.get('message_data', [])
            if m_type == 'private':
                private_list.extend(m_data)
            elif m_type == 'group':
                group_list.extend(m_data)

        if private_list:
            chat_section += "## 私人聊天话题\n"
            for item in private_list:
                time_str = item.get('时间') or '未知时间'
                topic = item.get('话题') or '无话题'
                summary = item.get('总结') or '无总结'
                chat_section += f"- [{time_str}] 话题: {topic} | 总结: {summary}\n"
        
        if group_list:
            chat_section += "\n## 群聊话题总结\n"
            # 按群组聚合
            groups = {}
            for item in group_list:
                g_name = item.get('群名称') or '未知群聊'
                if g_name not in groups: groups[g_name] = []
                groups[g_name].append(item)
            
            for g_name, topics in groups.items():
                chat_section += f"### 【{g_name}】\n"
                for t in topics:
                    time_str = t.get('时间') or '未知时间'
                    summary = t.get('话题总结') or t.get('总结') or '无总结'
                    chat_section += f"#### [{time_str}]\n{summary}\n\n"
        
        user_prompt = pv2.CHAT_USER_PROMPT.format(
            character_name=character_name,
            chat_section=chat_section,
            existing_items_section=existing_section
        )
    else: # title_summary
        user_prompt = pv2.TITLE_SUMMARY_USER_PROMPT.format(
            character_name=character_name,
            data_section=data_section,
            other_modules_section=f"\n# 各模块分析结论汇聚\n{other_modules_context}" if other_modules_context else "",
            memory_section=f"\n# 长期记忆/历史背景\n{long_term_memory_context}" if long_term_memory_context else ""
        )

    # 打印调试信息
    print(f"\n{'='*60}")
    print(f"DEBUG: Analyzing Module [{module_key}]")
    print(f"{'='*60}")
    print(f"\n--- [SYSTEM PROMPT] ---\n{system_prompt}")
    print(f"\n--- [USER PROMPT] ---\n{user_prompt}")
    print(f"\n{'='*60}\n")

    # 3. 调用 LLM
    try:
        response = client.messages.create(
            model=model,
            system=system_prompt,
            temperature=0.7,
            max_tokens=4000,
            messages=[{"role": "user", "content": user_prompt}]
        )
        raw_text = extract_text_from_anthropic_response(response)
        clean_text = _clean_markdown_wrapper(raw_text)
        result = _safe_json_loads(clean_text)
        
        if not result:
            return {"error": "Failed to parse LLM JSON response"}
            
        return result
    except Exception as e:
        logger.error(f"Module {module_key} analysis failed: {str(e)}")
        return {"error": str(e)}

def analyze_all_modules_sequential(
    aggregated_data,
    character_name,
    persona_info,
    previous_analysis_result=None,
    long_term_memory_context=None,
    target_module=None
):
    """
    顺序执行所有模块分析（V2 版本的核心入口）
    支持 target_module 参数，用于仅重新生成特定模块
    """
    # 预处理：从 aggregated_data 提取 meta 信息
    data_summary = aggregated_data # 假设结构一致
    
    # 获取旧的 sections（如果存在）
    prev_result = previous_analysis_result or {}
    prev_sections = prev_result.get('sections', {})
    
    # 初始化新的 sections
    new_sections = prev_sections.copy()
    
    # 模块列表 (按照用户要求的优化顺序)
    modules = ['schedule', 'activity', 'findings', 'chat', 'title_summary']
    
    for mod in modules:
        # 如果指定了目标模块且当前不是目标模块，则跳过
        if target_module and mod != target_module:
            logger.info(f"Skipping module {mod} (target is {target_module})")
            continue

        logger.info(f"Analyzing module: {mod}")
        
        # 增加跳过逻辑：如果模块是 chat 且没有聊天数据，直接跳过
        if mod == 'chat' and not data_summary.get('qq_messages'):
            logger.info("No chat data found, skipping 'chat' module.")
            new_sections[mod] = {
                "status": "skipped",
                "updated_at": timezone.now().isoformat()
            }
            continue

        # 构建上下文
        other_context = ""
        if mod == 'findings':
            # 有趣发现参考之前的作息和活动画像
            for m in ['schedule', 'activity']:
                if m in new_sections and new_sections[m].get('status') == 'done':
                    summary = new_sections[m].get('overall') or new_sections[m].get('summary')
                    if summary:
                        other_context += f"【{m} 模块结论】：{summary}\n"
        elif mod == 'title_summary':
            # 最终总结参考所有已生成的模块
            for m in ['schedule', 'activity', 'findings', 'chat']:
                if m in new_sections and new_sections[m].get('status') == 'done':
                    # 尝试获取最能代表该模块结论的字段
                    summary = new_sections[m].get('overall') or new_sections[m].get('summary') or new_sections[m].get('content')
                    if summary:
                        other_context += f"【{m} 模块结论】：{summary}\n"

        result = analyze_module_structured(
            mod,
            data_summary,
            character_name,
            persona_info,
            previous_module_data=prev_sections.get(mod),
            long_term_memory_context=long_term_memory_context,
            other_modules_context=other_context,
            compact_mode=(mod == 'title_summary')
        )
        
        if "error" not in result:
            # 标记为 done 并保存内容
            # 对于 schedule/activity，我们需要自动锁定过去的时段
            if mod in ['schedule', 'activity']:
                slots = result.get('slots', [])
                # 简单逻辑：如果时段已经过去 1 小时，标记为 locked
                # 实际生产中可能需要更精确的逻辑
                for s in slots:
                    # 如果 LLM 没有标记 locked，我们可以根据时间戳尝试标记
                    pass
            
            new_sections[mod] = {
                "status": "done",
                "content": result.get('content'), # 针对 findings/title/summary
                "title": result.get('title'),
                "summary": result.get('summary'),
                "overall": result.get('overall'),
                "slots": result.get('slots'),
                "items": result.get('items'),
                "finding_keys": result.get('finding_keys'),
                "updated_at": timezone.now().isoformat()
            }
        else:
            new_sections[mod] = {
                "status": "error",
                "error": result["error"],
                "updated_at": timezone.now().isoformat()
            }

    return {
        "version": 2,
        "markdown": prev_result.get('markdown', ''), # 保留旧的
        "sections": new_sections
    }
