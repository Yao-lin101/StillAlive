import json
import logging
from django.utils import timezone
from django.conf import settings

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



def _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str):
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
    if cutoff_time_str != '未知' and 'T00:00:00' not in cutoff_time_str:
        data_section += "\n**【系统强烈提示】当前这一天还没结束！数据只同步到了上述截止时间。你的分析必须处于“正在直播”的视角，评价时要用“截至目前”，绝对不能作结案陈词（比如“今天你一共就走了xx步”、“到这就收工了”），而是要推测他接下去会干嘛。**\n"
   
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
                language_style_section = f"""
## 语言风格
{language_style}
"""
            
            system_prompt = f"""{ai_identity_desc}
{language_style_section}
## 角色沉浸要求
- **始终保持你的角色身份**：无论输出什么内容，都要严格按照你的人设来说话和思考。
- **不要出戏**：绝对不要在回复中提及"根据人设"、"结合设定"、"规则要求"等话语。你就是这个角色本身。
- **你是老熟人**：你一直在暗中观察他，把已知的背景信息自然地当成你本来就知道的事实说出来。

## 分析规则
1. **基于数据**：基于实际数据进行合理怀疑与大胆推测（使用"可能"、"难道是"等词），但绝不凭空捏造。
2. **数据局限性**：
   - 只能获取**前台运行**的应用状态，无法获取后台状态。
   - 某个应用（如音乐、下载、视频）在数据中只出现一次，可能意味着它一直在后台运行。不要错误推断"只使用了一次"或"只用了几分钟"。
   - 步数是全天累计值（按小时分布的数据表示"截至该小时的总步数"），切勿将其误解为"单独某个小时走出的步数"然后进行累加计算。
3. **时区**：所有时间均为北京时间。
"""
        else:
            system_prompt = """你是一位毒舌但精准的生活数据分析专家，负责对用户的日常活动数据进行锐评式分析。

## 硬性约束
1. 语言风格：毒舌、尖锐、抽象、有梗。口语化，可适当使用网络流行语。多用 emoji 增加表现力（如🌙🌅🤔🤡💀📱）。
2. 分析原则：基于实际数据进行合理怀疑与大胆推测（使用"可能"、"难道是"等词），但绝不凭空捏造。
3. 数据局限性：
   - 只能获取**前台运行**的应用状态，无法获取后台状态。
   - 某个应用（如音乐、下载、视频）在数据中只出现一次，可能意味着它一直在后台运行。不要错误推断"只使用了一次"或"只用了几分钟"。
   - 步数是全天累计值（按小时分布的数据表示"截至该小时的总步数"），切勿将其误解为"单独某个小时走出的步数"然后进行累加计算。
4. 时区：所有时间均为北京时间。
5. 沉浸式扮演：绝对不要在回复中提及"根据人设"、"结合设定"、"规则要求"等出戏的话语。你是一个一直暗中观察他的老熟人，请把已知的人设背景自然地当成你本来就知道的事实说出来。
"""

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
            
            data_section = _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str)
            
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
            
            user_prompt = f"""这是你之前为用户 {character_name} 生成的日报（基于 {prev_time_str} 的数据写成）：

{clean_previous_report}

---

现在，系统获取了截至目前（{curr_time_str}）的当天【最新全量数据快照】。

**重点提示**：
1. 你需要对比旧日报，并在全量数据中**重点寻找和关注【{prev_time_str} 到 {curr_time_str}】这段时间内的“新活动”**。
2. 请基于最新的全量数据快照，将这些新活动自然地续写或融入到原有日报中，并更新全局统计结论。

**严格输出要求**：
1. **保持风格一致性**：必须保留原有的语气、口吻、角色设定和整体格式。
2. **数据更新**：用新数据替换旧数据，但不要改变原有结构。
3. **不要重写**：只更新和补充内容，不要完全重写整个日报丢失早前的细节。
4. **保持沉浸**：绝对不要提及"更新"、"新增数据"等词语，继续保持你的角色身份。
5. **绝对纯净**：绝对不要包含“好的”、“这是更新后的”等任何过渡或说明文字，直接输出 Markdown 内容本身。

{data_section}

请直接输出更新后的完整日报，保持原有风格。绝对不要输出任何开场白或解释性文字！
"""
        elif not is_incremental and previous_report and previous_report.strip():
            # 最终总结阶段：整合所有带有中间过程标题的旧日报
            import re
            clean_previous_report = re.sub(r'\n+---\n+\*数据截止至：.*?\*\s*$', '', previous_report.strip())
            
            data_section = _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str)
            
            user_prompt = f"""这是今天白天在这个用户不断产生新活动时，逐步更新出来的“直播式”过程日报（包含了许多中途发现和临时增加的小标题）：

{clean_previous_report}

---

现在，今天已经彻底结束。
这是当天的【最终全量数据快照】：

{data_section}

**最终全天总结整理要求**：
1. 请结合上述的最终全量数据，把这篇带有很多如“傍晚更新速报”、“最新发现”等中间过程小标题的报告，**提炼、重构成一份结构清晰、首尾呼应的最终全天日报**。
2. 消除所有“直播中”、“截至目前”、“推测他接下去会”等未完结语气，改成对这一整天的完整结案复盘。
3. 把白天发现的闪光点和数据异常（比如某时刻的突然爆发）巧妙地融入到统一的章节结构中（例如“作息分析”、“活动画像”等），**绝对不要保留“X点更新速报”这种中途产生的零碎小标题**，让整份报告看起来是一次性写成的。
4. 依然保持你的沉浸式角色设定！
5. **绝对纯净**：绝对不要包含“好的”、“这是重构后的全天报告”等任何无关的过渡或说明文字，直接输出完整的 Markdown 内容本身。
"""
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

            data_section = _build_data_section(data_summary, target_date_str, weekday_str, cutoff_time_str)
            user_prompt += data_section
                
            if has_custom_ai_persona:
                user_prompt += """
## 输出要求
请使用 Markdown 格式输出，**保持你的角色身份和语言风格**。

内容需包含以下几个方面（你可以根据自己的说话风格来组织，不需要严格使用以下标题）：
**用2-4个字概括今天的整体状态**：
**整体总结**：用1-2句话总结今日整体活动
**作息分析**：分析他的作息时间
**活动画像**：从应用使用情况推测他当前的状态
**有趣发现**：寻找反常时间点或行为进行推测。

**重要提示**：用你自己的方式来表达，保持你的人设和语言风格，不要因为格式要求而变得生硬。
"""
            else:
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


