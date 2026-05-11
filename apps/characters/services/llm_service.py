import json
import logging
import re
from django.utils import timezone
from django.conf import settings
from . import prompts as pv2
from .prompts import (
    DEFAULT_V2_CORE_IDENTITY, 
    DEFAULT_V2_TRAITS, 
    DEFAULT_V2_STYLE
)
from . import llm_utils as utils

logger = logging.getLogger(__name__)


# ── 模块分析内部辅助函数 (Module Analysis Helpers) ──────────────────

def _get_module_trap_rules(module_key):
    """根据模块类型获取对应的数据陷阱规则"""
    trap_rules = []
    if module_key == 'schedule':
        trap_rules.append(pv2.STEPS_V2_TRAP_RULE)
    elif module_key == 'activity':
        trap_rules.append(pv2.APP_STAY_V2_TRAP_RULE)
    elif module_key == 'chat':
        trap_rules.append(pv2.CHAT_V2_TRAP_RULE)
    elif module_key == 'findings':
        trap_rules.extend([pv2.STEPS_V2_TRAP_RULE, pv2.APP_STAY_V2_TRAP_RULE, pv2.CHAT_V2_TRAP_RULE])
    return trap_rules

def _build_module_system_prompt(module_key, character_name, persona_info, data_summary, meta_constraints, is_day_ended):
    """构建模块化分析的 System Prompt"""
    ai_persona = persona_info.get('ai_persona') or {}
    core_identity = ai_persona.get('core_identity') or DEFAULT_V2_CORE_IDENTITY
    personality_traits = ai_persona.get('personality_traits') or DEFAULT_V2_TRAITS
    language_style = ai_persona.get('language_style') or DEFAULT_V2_STYLE
    
    format_instr_map = {
        'title_summary': pv2.TITLE_SUMMARY_FORMAT_INSTRUCTIONS,
        'schedule': pv2.SCHEDULE_FORMAT_INSTRUCTIONS,
        'activity': pv2.ACTIVITY_FORMAT_INSTRUCTIONS,
        'findings': pv2.FINDINGS_RECAP_FORMAT_INSTRUCTIONS if is_day_ended else pv2.FINDINGS_MONITOR_FORMAT_INSTRUCTIONS,
        'chat': pv2.CHAT_FORMAT_INSTRUCTIONS,
    }
    
    common_rules = pv2.BASE_V2_ANALYSIS_RULES
    trap_rules = _get_module_trap_rules(module_key)
    if trap_rules:
        common_rules += "4. **数据局限性与推理指南**：\n" + "\n".join([f"   {rule}" for rule in trap_rules])

    return pv2.V2_STRUCTURED_SYSTEM_PROMPT.format(
        core_identity=core_identity,
        personality_traits=personality_traits,
        language_style=language_style,
        character_name=character_name,
        user_persona=persona_info.get('persona', '无'),
        user_persona_status=(
            f"更新于 {(timezone.now() - persona_info['persona_updated_at']).days} 天前" 
            if persona_info.get('persona_updated_at') else "初始设定"
        ),
        system_inferred_persona=persona_info.get('system_inferred_persona', '无'),
        common_rules=common_rules,
        report_mode="模块化增量更新",
        mode_hint="请按照指定的 JSON 格式输出，保持角色沉浸。",
        cutoff_time=data_summary.get('data_cutoff_time', '未知'),
        meta_instructions_section=f"# 特殊约束\n{meta_constraints.strip()}" if meta_constraints and meta_constraints.strip() else "",
        format_instructions=format_instr_map.get(module_key, "").strip()
    )

def _build_module_user_prompt(module_key, character_name, data_section, previous_module_data, other_modules_context, memory_context):
    """构建模块化分析的 User Prompt"""
    # 统一内存上下文格式化 (不再主动加换行)
    mem_section = f"# 长期记忆/历史背景\n{memory_context.strip()}" if memory_context and memory_context.strip() else ""

    if module_key == 'schedule' or module_key == 'activity':
        prev_data = previous_module_data or {}
        locked_slots = [s.copy() for s in prev_data.get('slots', []) if s.get('locked')]
        for s in locked_slots: s.pop('locked', None)
        
        existing_section = pv2.SCHEDULE_EXISTING_SLOTS_TEMPLATE.format(
            locked_slots_json=json.dumps(locked_slots, ensure_ascii=False, indent=2)
        ).strip()
        
        prompt_tmpl = pv2.SCHEDULE_USER_PROMPT if module_key == 'schedule' else pv2.ACTIVITY_USER_PROMPT
        return prompt_tmpl.format(
            character_name=character_name,
            data_section=data_section.strip(),
            existing_slots_section=existing_section,
            memory_section=mem_section
        )
    
    elif module_key == 'findings':
        return pv2.FINDINGS_USER_PROMPT.format(
            character_name=character_name, 
            data_section=data_section.strip(),
            memory_section=mem_section
        )
    
    elif module_key == 'chat':
        prev_data = previous_module_data or {}
        locked_items = prev_data.get('items', [])
        existing_section = pv2.CHAT_EXISTING_ITEMS_TEMPLATE.format(
            locked_items_json=json.dumps(locked_items, ensure_ascii=False, indent=2)
        ).strip()
        
        return pv2.CHAT_USER_PROMPT.format(
            character_name=character_name,
            chat_section=data_section.strip(), 
            existing_items_section=existing_section,
            memory_section=mem_section
        )
    
    else: # title_summary
        return pv2.TITLE_SUMMARY_USER_PROMPT.format(
            character_name=character_name,
            data_section=data_section.strip(),
            other_modules_section=(f"# 各模块分析结论汇聚\n{other_modules_context.strip()}" if other_modules_context and other_modules_context.strip() else "")
        )

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
    redaction_items=None,
    is_day_ended=False
):
    """单模块结构化分析核心函数"""
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    if not api_key: return {"error": "API Key not configured"}

    # 1. 准备 Prompts
    system_prompt = _build_module_system_prompt(
        module_key, character_name, persona_info, data_summary, meta_constraints, is_day_ended
    )
    
    if module_key == 'chat':
        data_content = utils.format_chat_logs(data_summary)
    else:
        data_content = utils.build_data_section(
            data_summary, data_summary.get('date'), data_summary.get('data_cutoff_time'), 
            is_day_ended=is_day_ended,
            exclude_apps=(module_key == 'schedule'),
            exclude_steps=False,  # 所有模块默认开启步数，除非有特殊需求
            exclude_active_ranges=(module_key == 'activity'), # 活动画像模块隐藏冗长的活跃周期，专注于步数和App
            compact_mode=compact_mode
        )
    
    user_prompt = _build_module_user_prompt(
        module_key, character_name, data_content, previous_module_data, other_modules_context, long_term_memory_context
    )
    
    # 2. 脱敏与强化提示
    user_prompt = utils.apply_redactions(user_prompt, redaction_items)
    if meta_constraints and meta_constraints.strip():
        user_prompt += f"**再次提醒**：请务必检查并严格遵守以下【特殊约束】，确保输出内容符合用户的最新指示：\n{meta_constraints}"

    # 3. 打印调试信息 (恢复被误删的部分)
    print(f"\n{'='*60}")
    print(f"DEBUG: Analyzing Module [{module_key}]")
    print(f"{'='*60}")
    print(f"\n--- [SYSTEM PROMPT] ---\n{system_prompt}")
    print(f"\n--- [USER PROMPT] ---\n{user_prompt}")
    print(f"\n{'='*60}\n")

    # 4. 调用 LLM
    try:
        import anthropic
        base_url = getattr(settings, 'ANTHROPIC_BASE_URL', None)
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url) if base_url else anthropic.Anthropic(api_key=api_key)
        
        response = client.messages.create(
            model=getattr(settings, 'ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022'),
            system=system_prompt,
            temperature=0.7,
            max_tokens=8192,
            messages=[{"role": "user", "content": user_prompt}]
        )
        
        result = utils.safe_json_loads(utils.clean_markdown_wrapper(utils.extract_text_from_response(response)))
        return result or {"error": "Failed to parse LLM JSON response"}
    except Exception as e:
        logger.error(f"Module {module_key} analysis failed: {str(e)}")
        return {"error": str(e)}


# ── 流程编排内部辅助函数 (Workflow Helpers) ────────────────────

def _extract_meta_instructions(client, model, private_blocks, data_keys=[]):
    """从私聊记录中提取元指令和脱敏需求 (采用两步审计法)"""
    if not private_blocks:
        return {"instructions": [], "redactions": [], "has_any": False}

    # 格式化私聊记录
    chat_content = ""
    for block in private_blocks:
        if '用户' in block: chat_content += f"用户: {block['用户']}\n"
        if '你的回复' in block: chat_content += f"你: {block['你的回复']}\n"
        if '话题' in block: chat_content += f"话题: {block['话题']}\n"
        if '总结' in block: chat_content += f"总结: {block['总结']}\n"
        chat_content += "---\n"

    try:
        print("\n" + "="*30 + " [AUDIT STAGE 1: INTENT] " + "="*30)
        prompt_step1 = pv2.META_INSTRUCTION_CHECK_PROMPT.format(private_chat_content=chat_content)
        response_step1 = client.messages.create(
            model=model, max_tokens=1000, temperature=0,
            messages=[{"role": "user", "content": prompt_step1}]
        )
        raw_res1 = utils.extract_text_from_response(response_step1)
        print(f"STAGE 1 RAW RESPONSE:\n{raw_res1}")
        res1 = utils.safe_json_loads(raw_res1) or {"instructions": [], "needs_redaction": False}
        
        instructions = res1.get('instructions', [])
        needs_redaction = res1.get('needs_redaction', False)
        print(f"STAGE 1 DECISION: Instructions={instructions}, NeedsRedaction={needs_redaction}")
        redactions = []

        # --- 步骤 2: 精准脱敏匹配 ---
        if needs_redaction and data_keys:
            sorted_keys = sorted(data_keys)
            data_keys_str = "\n".join([f"- {k}" for k in sorted_keys])
            prompt_step2 = pv2.META_REDACTION_MATCH_PROMPT.format(
                private_chat_content=chat_content, data_keys=data_keys_str
            )
            print("\n" + "="*30 + " [AUDIT STAGE 2: REDACTION] " + "="*30)
            response_step2 = client.messages.create(
                model=model, max_tokens=2000, temperature=0,
                messages=[{"role": "user", "content": prompt_step2}]
            )
            raw_res2 = utils.extract_text_from_response(response_step2)
            print(f"STAGE 2 RAW RESPONSE:\n{raw_res2}")
            redactions = utils.safe_json_loads(raw_res2) or []
            if not isinstance(redactions, list): redactions = []
            print(f"STAGE 2 DECISION: Redactions={redactions}")
        
        return {"instructions": instructions, "redactions": redactions, "has_any": (bool(instructions) or bool(redactions))}
    except Exception as e:
        logger.error(f"Audit stage failed: {str(e)}")
        return {"instructions": [], "redactions": [], "has_any": False}

def _perform_audit_stage(data_summary):
    """第一阶段：审计与脱敏识别"""
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    model = getattr(settings, 'ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')
    base_url = getattr(settings, 'ANTHROPIC_BASE_URL', None)
    
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, base_url=base_url) if base_url else anthropic.Anthropic(api_key=api_key)

    private_blocks = []
    for msg_record in data_summary.get('qq_messages', []):
        if isinstance(msg_record, dict) and msg_record.get('message_type') == 'private':
            private_blocks.extend(msg_record.get('message_data', []))
    
    data_keys = utils.get_redaction_keys(data_summary)
    audit_result = _extract_meta_instructions(client, model, private_blocks, data_keys)
    
    return {
        "meta_instructions": "\n".join(audit_result.get('instructions', [])),
        "redaction_items": audit_result.get('redactions', [])
    }

def _merge_module_result(module_key, result, prev_mod_data, is_day_ended, extra_meta):
    """将 LLM 返回的结果与现有数据进行合并与锁定逻辑处理"""
    prev_mod_data = prev_mod_data or {}
    
    if module_key in ['schedule', 'activity', 'findings']:
        final_slots = [s for s in prev_mod_data.get('slots', []) if s.get('locked')]
        new_slots = result.get('new_slots') or result.get('slots') or []
        
        if isinstance(new_slots, list):
            final_slots.extend(new_slots)
            # 锁定规则：除了最后一个全锁定
            if module_key in ['schedule', 'activity']:
                for i, s in enumerate(final_slots):
                    s['locked'] = (i < len(final_slots) - 1)
            else: # findings
                if is_day_ended: final_slots = new_slots # 结项分析重写
                else: final_slots = prev_mod_data.get('slots', []) # 白天保留
        
        return {
            "status": "done",
            "overall": result.get('overall') or prev_mod_data.get('overall'),
            "slots": final_slots,
            "updated_at": timezone.now().isoformat(),
            **extra_meta
        }

    elif module_key == 'chat':
        final_items = prev_mod_data.get('items', [])
        new_items = result.get('new_items') or result.get('items') or []
        if isinstance(new_items, list): final_items.extend(new_items)
        
        return {
            "status": "done",
            "overall": result.get('overall') or prev_mod_data.get('overall'),
            "items": final_items,
            "updated_at": timezone.now().isoformat(),
            **extra_meta
        }

    else: # title_summary
        return {
            "status": "done",
            "title": result.get('title') or prev_mod_data.get('title'),
            "summary": result.get('summary') or prev_mod_data.get('summary'),
            "updated_at": timezone.now().isoformat(),
            **extra_meta
        }

def analyze_all_modules_sequential(
    aggregated_data,
    character_name,
    persona_info,
    previous_analysis_result=None,
    long_term_memory_context=None,
    target_modules=None,
    on_module_complete=None,
    is_day_ended=False,
    incremental=False
):
    """顺序执行所有模块分析 (重构版)"""
    # 1. 审计阶段
    audit = _perform_audit_stage(aggregated_data)
    
    prev_result = previous_analysis_result or {}
    prev_sections = prev_result.get('sections', {})
    new_sections = prev_sections.copy()
    
    modules = ['schedule', 'activity', 'findings', 'chat', 'title_summary']
    
    for mod in modules:
        # 模块过滤逻辑
        is_target = target_modules is None or (
            (isinstance(target_modules, str) and target_modules == mod) or
            (isinstance(target_modules, (list, tuple)) and mod in target_modules)
        )
        
        if not is_target:
            continue

        logger.info(f"Analyzing module: {mod}")
        
        # 增量检测与跳过逻辑
        extra_meta = {}
        if mod == 'chat':
            messages = aggregated_data.get('qq_messages', [])
            if not messages:
                new_sections[mod] = {"status": "skipped", "updated_at": timezone.now().isoformat()}
                continue
            current_msg_count = sum(len(b.get('message_data', [])) for b in messages)
            prev_chat = prev_sections.get('chat', {})
            # 只有在非目标模块且消息数量未变化时才跳过
            # 注意：此处 is_target 已定义。由于上面已经 continue 掉了非 target 模块，
            # 这里的 is_target 其实始终为 True，但逻辑上保留这种判断更健壮。
            # 如果我们希望在“全量重跑模式”下不跳过，可以使用 incremental 标志。
            if incremental and prev_chat.get('status') == 'done' and prev_chat.get('_msg_count') == current_msg_count:
                continue
            extra_meta = {'_msg_count': current_msg_count}
        
        # 构建模块间上下文
        other_context = ""
        if mod == 'title_summary':
            for m in ['schedule', 'activity', 'findings', 'chat']:
                if m in new_sections and new_sections[m].get('status') == 'done':
                    summary = new_sections[m].get('overall') or new_sections[m].get('summary') or new_sections[m].get('content')
                    if summary: other_context += f"【{m} 模块结论】：{summary}\n"
        if incremental:
            # 增量模式：始终尝试获取旧数据
            mod_prev = prev_sections.get(mod)
        else:
            # 非增量模式：如果是目标重跑模块，则清空旧数据以强制全量重跑；否则保留旧数据（跳过重跑）
            mod_prev = None if is_target else prev_sections.get(mod)

        result = analyze_module_structured(
            mod, aggregated_data, character_name, persona_info,
            previous_module_data=mod_prev,
            meta_constraints=audit['meta_instructions'],
            long_term_memory_context=long_term_memory_context,
            other_modules_context=other_context,
            compact_mode=(mod == 'title_summary'),
            redaction_items=audit['redaction_items'],
            is_day_ended=is_day_ended
        )
        
        # 合并结果
        if "error" not in result:
            new_sections[mod] = _merge_module_result(mod, result, mod_prev, is_day_ended, extra_meta)
        else:
            # --- 核心修复：更严格的旧数据保护逻辑 ---
            # 只要 prev_sections 里有数据（无论 status 是什么），都应该保留
            if mod in prev_sections:
                old_data = prev_sections[mod].copy()
                old_data.update({
                    'last_error': result["error"],
                    'updated_at': timezone.now().isoformat(),
                    'status': old_data.get('status', 'error') # 保持原状态，除非原来就没状态
                })
                new_sections[mod] = old_data
                logger.warning(f"Module {mod} analysis failed, preserved existing data. Error: {result['error']}")
            else:
                new_sections[mod] = {"status": "error", "error": result["error"], "updated_at": timezone.now().isoformat()}

        if on_module_complete:
            on_module_complete(new_sections)

    return {"version": 2, "markdown": prev_result.get('markdown', ''), "sections": new_sections}
