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
    compact_mode=False,
    redaction_items=None
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
    trap_rules = []
    
    if module_key == 'schedule':
        trap_rules.append(pv2.STEPS_V2_TRAP_RULE)
    elif module_key == 'activity':
        trap_rules.append(pv2.APP_STAY_V2_TRAP_RULE)
    elif module_key == 'chat':
        trap_rules.append(pv2.CHAT_V2_TRAP_RULE)
    elif module_key == 'findings':
        # 有趣发现涉及全量原始数据，注入所有陷阱提示以防误判
        trap_rules.extend([
            pv2.STEPS_V2_TRAP_RULE,
            pv2.APP_STAY_V2_TRAP_RULE,
            pv2.CHAT_V2_TRAP_RULE
        ])
    elif module_key == 'title_summary':
        # 最终总结基于各模块结论和精简摘要，无需底层数据陷阱提示
        pass

    if trap_rules:
        common_rules += "4. **数据局限性与推理指南**：\n" + "\n".join([f"   {rule}" for rule in trap_rules])

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
        # 提取已锁定的 slots (排除最后一位未锁定的)
        prev_data = previous_module_data or {}
        all_slots = prev_data.get('slots', [])
        locked_slots = []
        for s in all_slots:
            if s.get('locked'):
                # 注入提示词时移除 locked 标记以减少干扰
                s_copy = s.copy()
                s_copy.pop('locked', None)
                locked_slots.append(s_copy)
        
        existing_section = pv2.SCHEDULE_EXISTING_SLOTS_TEMPLATE.format(
            locked_slots_json=json.dumps(locked_slots, ensure_ascii=False, indent=2)
        )
        user_prompt = pv2.SCHEDULE_USER_PROMPT.format(
            character_name=character_name,
            data_section=data_section,
            existing_slots_section=existing_section
        )
    elif module_key == 'activity':
        prev_data = previous_module_data or {}
        all_slots = prev_data.get('slots', [])
        locked_slots = []
        for s in all_slots:
            if s.get('locked'):
                s_copy = s.copy()
                s_copy.pop('locked', None)
                locked_slots.append(s_copy)
        
        existing_section = pv2.SCHEDULE_EXISTING_SLOTS_TEMPLATE.format( # 复用模板
            locked_slots_json=json.dumps(locked_slots, ensure_ascii=False, indent=2)
        )
        user_prompt = pv2.ACTIVITY_USER_PROMPT.format(
            character_name=character_name,
            data_section=data_section,
            existing_slots_section=existing_section
        )
    elif module_key == 'findings':
        prev_data = previous_module_data or {}
        all_slots = prev_data.get('slots', [])
        # 发现模块全量注入已锁定的发现，防止重复
        locked_slots = []
        for s in all_slots:
            if s.get('locked'):
                s_copy = s.copy()
                s_copy.pop('locked', None)
                locked_slots.append(s_copy)

        existing_section = pv2.SCHEDULE_EXISTING_SLOTS_TEMPLATE.format(
            locked_slots_json=json.dumps(locked_slots, ensure_ascii=False, indent=2)
        )
        user_prompt = pv2.FINDINGS_USER_PROMPT.format(
            character_name=character_name,
            data_section=data_section,
            existing_slots_section=existing_section
        )
    elif module_key == 'chat':
        # 聊天模块增量更新，注入已分析的话题
        prev_data = previous_module_data or {}
        all_items = prev_data.get('items', [])
        locked_items = []
        for item in all_items:
            # 聊天记录默认入库即锁定
            locked_items.append(item)
            
        existing_section = pv2.CHAT_EXISTING_ITEMS_TEMPLATE.format(
            locked_items_json=json.dumps(locked_items, ensure_ascii=False, indent=2)
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
            chat_section += "## 私人聊天内容\n"
            for item in private_list:
                time_str = item.get('时间') or '未知时间'
                chat_section += f"[{time_str}]\n"
                
                # 1. 话题与总结
                topic = item.get('话题')
                summary = item.get('总结')
                if topic: chat_section += f"话题: {topic}\n"
                if summary: chat_section += f"总结: {summary}\n"
                
                # 2. 对话细节
                user_msg = item.get('用户')
                bot_msg = item.get('你的回复')
                if user_msg:
                    chat_section += f"用户: {user_msg}\n"
                if bot_msg:
                    # 截断过长的机器人回复
                    if len(bot_msg) > 100:
                        lines = bot_msg.split('\n')
                        truncated = lines[0] + '...' if lines else bot_msg[:50] + '...'
                        chat_section += f"你: {truncated}\n"
                    else:
                        chat_section += f"你: {bot_msg}\n"
                chat_section += "\n"
        
        if group_list:
            chat_section += "\n## 群聊内容总结\n"
            # 按群组聚合
            groups = {}
            for item in group_list:
                g_name = item.get('群名称') or '未知群聊'
                if g_name not in groups: 
                    groups[g_name] = {
                        'bot_nickname': item.get('你在本群昵称', '未知'),
                        'user_nickname': item.get('用户在本群昵称', '未知'),
                        'topics': []
                    }
                groups[g_name]['topics'].append(item)
            
            for g_name, info in groups.items():
                chat_section += f"### 【{g_name}】\n"
                chat_section += f"- 你的群昵称: {info['bot_nickname']}\n"
                chat_section += f"- 用户的群昵称: {info['user_nickname']}\n\n"
                for t in info['topics']:
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

    # 4. 执行数据脱敏 (Data Redaction)
    if redaction_items:
        mask_text = "【隐藏剧情】"
        for original in redaction_items:
            if original and original.strip():
                user_prompt = user_prompt.replace(original, mask_text)

    # 5. 动态追加末尾强化提醒 (针对特殊约束内容复述)
    if meta_constraints and meta_constraints.strip():
        user_prompt += f"\n\n**再次提醒**：请务必检查并严格遵守以下【特殊约束】，确保输出内容符合用户的最新指示：\n{meta_constraints}"

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
    target_modules=None,
    on_module_complete=None
):
    """
    顺序执行所有模块分析（V2 版本的核心入口）
    支持 target_modules 参数，可以是字符串或列表，用于仅重新生成特定模块
    """
    # 预处理：从 aggregated_data 提取 meta 信息
    data_summary = aggregated_data 
    
    # 1. 审计阶段 (Stage 1: Audit & Redaction)
    # 获取 client/model 以便调用审计工具
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    model = getattr(settings, 'ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')
    base_url = getattr(settings, 'ANTHROPIC_BASE_URL', None)
    
    import anthropic
    client_kwargs = {'api_key': api_key}
    if base_url: client_kwargs['base_url'] = base_url
    client = anthropic.Anthropic(**client_kwargs)

    private_blocks = []
    for msg_record in data_summary.get('qq_messages', []):
        if isinstance(msg_record, dict) and msg_record.get('message_type') == 'private':
            private_blocks.extend(msg_record.get('message_data', []))
    
    data_keys = _get_data_keys(data_summary)
    audit_result = _extract_meta_instructions(client, model, private_blocks, data_keys)
    
    meta_instructions = "\n".join(audit_result.get('instructions', []))
    redaction_items = audit_result.get('redactions', [])

    # 获取旧的 sections（如果存在）
    prev_result = previous_analysis_result or {}
    prev_sections = prev_result.get('sections', {})
    
    # 初始化新的 sections
    new_sections = prev_sections.copy()
    
    # 模块列表 (按照用户要求的优化顺序)
    modules = ['schedule', 'activity', 'findings', 'chat', 'title_summary']
    
    for mod in modules:
        # 如果指定了 target_modules 且不是当前模块，则跳过
        if target_modules:
            if isinstance(target_modules, str) and target_modules != mod:
                continue
            if isinstance(target_modules, (list, tuple)) and mod not in target_modules:
                continue

        logger.info(f"Analyzing module: {mod}")
        
        # 增加跳过逻辑：如果模块是 chat 且没有聊天数据，直接跳过
        if mod == 'chat':
            messages = data_summary.get('qq_messages', [])
            if not messages:
                logger.info("No chat data found, skipping 'chat' module.")
                new_sections[mod] = {
                    "status": "skipped",
                    "updated_at": timezone.now().isoformat()
                }
                continue
            
            # 增量检测：如果消息总数没变，且之前已经分析完成，则跳过
            prev_chat = prev_sections.get('chat', {})
            current_msg_count = sum(len(b.get('message_data', [])) for b in messages)
            if prev_chat.get('status') == 'done' and prev_chat.get('_msg_count') == current_msg_count:
                logger.info(f"Chat data unchanged (count: {current_msg_count}), skipping re-analysis.")
                continue
            
            # 准备在新 section 中记录当前消息数
            # 注意：这里先标记，实际数据在分析后存入
            # 但为了逻辑一致性，我们在分析前记录
            mod_extra_meta = {'_msg_count': current_msg_count}
        else:
            mod_extra_meta = {}

        # 构建上下文
        other_context = ""
        if mod == 'title_summary':
            # 最终总结参考所有已生成的模块
            for m in ['schedule', 'activity', 'findings', 'chat']:
                if m in new_sections and new_sections[m].get('status') == 'done':
                    # 尝试获取最能代表该模块结论的字段
                    summary = new_sections[m].get('overall') or new_sections[m].get('summary') or new_sections[m].get('content')
                    if summary:
                        other_context += f"【{m} 模块结论】：{summary}\n"

        # 如果是目标重跑模块，不传入旧的分析结果，实现“不带入旧数据”
        is_target = target_modules and (
            (isinstance(target_modules, str) and target_modules == mod) or
            (isinstance(target_modules, (list, tuple)) and mod in target_modules)
        )
        mod_prev = None if is_target else prev_sections.get(mod)

        result = analyze_module_structured(
            mod,
            data_summary,
            character_name,
            persona_info,
            previous_module_data=mod_prev,
            meta_constraints=meta_instructions,
            long_term_memory_context=long_term_memory_context,
            other_modules_context=other_context,
            compact_mode=(mod == 'title_summary'),
            redaction_items=redaction_items
        )
        
        if "error" not in result:
            # --- 增量合并与锁定逻辑 ---
            prev_mod_data = prev_sections.get(mod) or {}
            
            if mod in ['schedule', 'activity', 'findings']:
                # 提取之前已锁定的
                final_slots = [s for s in prev_mod_data.get('slots', []) if s.get('locked')]
                # 获取 LLM 返回的新 slots (V2 推荐使用 new_slots 字段，兼容旧版 slots)
                new_slots = result.get('new_slots') or result.get('slots') or []
                
                if isinstance(new_slots, list):
                    # 合并
                    final_slots.extend(new_slots)
                    
                    # 应用锁定规则
                    if mod in ['schedule', 'activity']:
                        # 除了最后一个，全部锁定
                        for i, s in enumerate(final_slots):
                            s['locked'] = (i < len(final_slots) - 1)
                    else: # findings
                        # 全部锁定
                        for s in final_slots:
                            s['locked'] = True
                
                new_sections[mod] = {
                    "status": "done",
                    "overall": result.get('overall') or prev_mod_data.get('overall'),
                    "slots": final_slots,
                    "updated_at": timezone.now().isoformat(),
                    **mod_extra_meta
                }
                # 发现模块额外处理关键词
                if mod == 'findings':
                    old_keys = prev_mod_data.get('finding_keys', [])
                    new_keys = result.get('new_finding_keys') or result.get('finding_keys') or []
                    new_sections[mod]['finding_keys'] = list(set(old_keys + new_keys))

            elif mod == 'chat':
                final_items = prev_mod_data.get('items', []) # 聊天记录默认都是锁定的
                new_items = result.get('new_items') or result.get('items') or []
                
                if isinstance(new_items, list):
                    final_items.extend(new_items)
                
                new_sections[mod] = {
                    "status": "done",
                    "overall": result.get('overall') or prev_mod_data.get('overall'),
                    "items": final_items,
                    "updated_at": timezone.now().isoformat(),
                    **mod_extra_meta
                }

            else: # title_summary
                new_sections[mod] = {
                    "status": "done",
                    "title": result.get('title') or prev_mod_data.get('title'),
                    "summary": result.get('summary') or prev_mod_data.get('summary'),
                    "updated_at": timezone.now().isoformat(),
                    **mod_extra_meta
                }
        else:
            new_sections[mod] = {
                "status": "error",
                "error": result["error"],
                "updated_at": timezone.now().isoformat()
            }

        # 增量回调：每完成一个模块就通知调用方
        if on_module_complete:
            on_module_complete(new_sections)

    return {
        "version": 2,
        "markdown": prev_result.get('markdown', ''), # 保留旧的
        "sections": new_sections
    }
