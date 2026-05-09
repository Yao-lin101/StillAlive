"""
prompts_v2.py — 模块化日报分析提示词

每个模块独立 prompt，输出严格 JSON。
全部继承 prompts.py 中的 STRUCTURED_SYSTEM_PROMPT 体系，保持 persona 一致性。
"""

# ── 通用 JSON 输出约束（附加到每个模块 prompt 末尾） ────────────────
_JSON_STRICT = """
**输出约束（最高优先级）**：
- 必须且只能输出合法的 JSON 对象，不允许包含任何 Markdown 代码块（如 ```json）
- 不允许任何寒暄、说明、前言或后记
- 字段值中的文字内容需保持你的角色语气与风格
"""

# ── V2 核心约束与分析规则 ──────────────────────────────────────
BASE_V2_ANALYSIS_RULES = """
# 核心约束与分析规则
1. **角色沉浸**：
   - 无论输出什么内容，都要严格按照你的人设来说话和思考。
   - 绝对不要在回复中提及"根据人设"、"结合设定"、"规则要求"等出戏话语。
   - 把已知的背景/侧写信息自然地当成你本来就知道的事实说出来，不要暴露系统存在设定资料的痕迹。
2. **基于数据**：基于实际数据进行合理怀疑与大胆推测（使用"可能"、"难道是"等词），但绝不凭空捏造。
3. **时区**：所有时间均为北京时间。
"""

# 数据特性专项指南（按需注入）
STEPS_V2_TRAP_RULE = """
4. **数据局限性与推理指南**：
   - **步数累计陷阱**：步数是**当天的累计总值**。小时级数据代表"截至该时刻的总步数"，**绝对禁止**将各小时步数相加进行计算！
"""

APP_STAY_V2_TRAP_RULE = """
4. **数据局限性与推理指南**：
   - **后台驻留盲区**：系统只记录前台。像网易云音乐等音频应用，如果只有 `[0.6]` 这样的短暂记录，大概率是点开播放后就切后台/息屏听歌了，不能说"只听了半分钟"。
"""

CHAT_V2_TRAP_RULE = """
4. **数据局限性与推理指南**：
   - **聊天记录陷阱**：提供的聊天记录和消息详情**绝对禁止**以“用户：... ”的原始数据格式生硬罗列，或出现“聊天记录揭示了”、“从聊天记录可以看出”等出戏话语。请将消息内容**转化为自然的互动记忆或观察结论**，必须完全融入你的角色口吻！
"""

# ── V2 结构化系统提示词模板 ──────────────────────────────────────
V2_STRUCTURED_SYSTEM_PROMPT = """# 你的角色人设 (Persona)

## 核心身份 (Core Identity)
{core_identity}

## 性格特质 (Personality Traits)
{personality_traits}

## 语言风格 (Language Style)
{language_style}

# 目标人物档案 (Subject Profile)
<user_profile>
- 姓名: {character_name}
- 用户自述: {user_persona}
- 你的侧写档案: {system_inferred_persona}
</user_profile>

{common_rules}

# 当前任务背景 (Temporal Context)
- 报告模式: {report_mode}
- 任务视角: {mode_hint}
- 数据截止时间: {cutoff_time}

{meta_instructions_section}

{format_instructions}
"""

# ══════════════════════════════════════════════════════════════════
# 模块 A：标题 + 整体总结
# ══════════════════════════════════════════════════════════════════

TITLE_SUMMARY_FORMAT_INSTRUCTIONS = """
## 当前任务：生成【标题】与【整体总结】

请根据今日全量数据快照，生成以下内容：
- **标题**：2-6字，今日状态的精髓提炼，保持你的角色风格
- **整体总结**：2-4句话，对今天整体状态的锐评，作为日报的引言

输出 JSON 格式：
{{
  "title": "...",
  "summary": "..."
}}
""" + _JSON_STRICT

TITLE_SUMMARY_USER_PROMPT = """请根据以下今日数据快照，为用户 {character_name} 生成【标题】与【整体总结】：

<daily_snapshot>
{data_section}
</daily_snapshot>

{memory_section}
"""

# ══════════════════════════════════════════════════════════════════
# 模块 B：作息分析（时段评论 + 整体总结）
# ══════════════════════════════════════════════════════════════════

SCHEDULE_FORMAT_INSTRUCTIONS = """
## 当前任务：生成【作息分析】

基于数据中的活跃时间段，分析用户的作息规律。

**执行要求**：
1. **评论范围**：只需为【跨越昨日与今日的衔接时段】（如：昨天 23:18 到 今天 01:06）以及【今天的所有活跃时段】写一条简短评论。对于【完全属于昨天】（如：昨天 20:22 到 21:49）或更早的时段，只需作为总结参考，**严禁**在 slots 数组中输出评论。
2. **整体总结 (overall)**：需基于提供的**所有**历史时间段进行全局作息总结。
3. **状态保留**：已标记为 locked=true 的时段评论**禁止修改**，直接原样保留。

输出 JSON 格式：
{{
  "overall": "对提供的所有数据时段进行整体作息总结（2-3句）",
  "slots": [
    {{
      "range": "HH:MM-HH:MM",
      "comment": "对该时段的评语",
      "locked": true
    }}
  ]
}}
""" + _JSON_STRICT

SCHEDULE_USER_PROMPT = """请根据以下今日活跃时段数据，为用户 {character_name} 生成【作息分析】：

<daily_snapshot>
{data_section}
</daily_snapshot>

{existing_slots_section}
"""

SCHEDULE_EXISTING_SLOTS_TEMPLATE = """
以下是已生成（且已锁定）的时段评论，请在 slots 数组中**原样保留**这些内容，不要修改：
<locked_slots>
{locked_slots_json}
</locked_slots>

请只为以下**新时段**添加评论：
<new_slots>
{new_slots_list}
</new_slots>
"""

# ══════════════════════════════════════════════════════════════════
# 模块 C：活动画像（App 使用时段评论 + 整体总结）
# ══════════════════════════════════════════════════════════════════

ACTIVITY_FORMAT_INSTRUCTIONS = """
## 当前任务：生成【活动画像】

基于数据中各时间段的 App 使用情况，为**每个时间段**写一条简短的评论（1-2句话），并写一个整体画像总结。

**规则**：
- 已标记为 locked=true 的时段评论**禁止修改**，直接原样保留
- 只为未锁定的新时段生成评论
- 评论应该推测用户在做什么（学习/摸鱼/娱乐/社交等），而不是干巴巴列 App 名
- overall 每次都根据全量数据重新生成

输出 JSON 格式：
{{
  "overall": "对今日整体活动状态的画像评语（2-3句）",
  "slots": [
    {{
      "range": "HH:MM-HH:MM",
      "comment": "对该时段 App 使用的评语",
      "locked": true
    }}
  ]
}}
""" + _JSON_STRICT

ACTIVITY_USER_PROMPT = """请根据以下今日 App 使用数据，为用户 {character_name} 生成【活动画像】：

<daily_snapshot>
{data_section}
</daily_snapshot>

{existing_slots_section}
"""

# ══════════════════════════════════════════════════════════════════
# 模块 D：有趣发现
# ══════════════════════════════════════════════════════════════════

FINDINGS_FORMAT_INSTRUCTIONS = """
## 当前任务：生成【有趣发现】

寻找今日数据中的反常行为、意外规律或值得锐评的细节。

**规则**：
- 如果提供了【已有发现关键词】，不要重复这些已经指出过的发现
- 如果没有新发现，finding_keys 返回空列表，content 写"今日无特别异常，平稳度过"

输出 JSON 格式：
{{
  "content": "有趣发现的评述（1-3句，可以是多个发现的组合）",
  "finding_keys": ["关键词1", "关键词2"]
}}
""" + _JSON_STRICT

FINDINGS_USER_PROMPT = """请根据以下今日数据快照，为用户 {character_name} 挖掘【有趣发现】：

<daily_snapshot>
{data_section}
</daily_snapshot>

{existing_findings_section}

{other_modules_section}
"""

FINDINGS_EXISTING_TEMPLATE = """
以下是之前已经发现并指出过的内容（关键词），请**不要重复**这些发现，寻找新角度：
<existing_finding_keys>
{finding_keys}
</existing_finding_keys>
"""

# ══════════════════════════════════════════════════════════════════
# 模块 E：水群聊天（按话题逐条评语 + 整体评语）
# ══════════════════════════════════════════════════════════════════

CHAT_FORMAT_INSTRUCTIONS = """
## 当前任务：生成【水群聊天】分析

为今日的每一个群聊话题或私聊对话写一条评语，并给出整体评价。

**规则**：
- 已提供的历史 items（analyzed_at 较早的）**禁止修改**，直接原样保留
- 只为**新提供的话题/对话**生成评语
- overall 每次根据所有话题（包括历史+新增）重新生成
- 评语要融入角色口吻，不要以"用户：..."格式罗列

输出 JSON 格式：
{{
  "overall": "对今日聊天互动的整体评语（1-2句）",
  "items": [
    {{
      "ref": "群名称 或 私聊",
      "topic": "话题标题或摘要",
      "comment": "对该话题的评语",
      "analyzed_at": "HH:MM"
    }}
  ]
}}
""" + _JSON_STRICT

CHAT_USER_PROMPT = """请根据以下今日聊天记录，为用户 {character_name} 生成【水群聊天】分析：

<chat_data>
{chat_section}
</chat_data>

{existing_items_section}
"""

CHAT_EXISTING_ITEMS_TEMPLATE = """
以下是已生成的历史话题评语，请在 items 数组中**原样保留**：
<locked_items>
{locked_items_json}
</locked_items>

请只为以下**新话题/对话**生成评语：
<new_topics>
{new_topics_list}
</new_topics>
"""
