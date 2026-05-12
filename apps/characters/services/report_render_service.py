"""
日报 Markdown 渲染服务

负责将 DailyReport.analysis_result (V2 结构化 JSON) 渲染为
易于 AI 阅读和用户查看的完整 Markdown 字符串。
"""

def render_v2_report_to_markdown(analysis_result: dict) -> str:
    """
    将 V2 版本的结构化日报数据渲染为完整的 Markdown
    """
    if not analysis_result:
        return ""
    
    # 如果版本低于 2，尝试直接返回原始 markdown
    version = analysis_result.get('version', 1)
    if version < 2:
        return analysis_result.get('markdown', '')

    sections = analysis_result.get('sections', {})
    if not sections:
        return analysis_result.get('markdown', '')

    lines = []

    # 1. 标题与总览 (Title & Summary)
    ts = sections.get('title_summary', {})
    title = ts.get('title')
    summary = ts.get('summary')
    
    if title:
        lines.append(f"# {title}")
    
    if summary:
        if lines: lines.append("") # 间隔
        lines.append(f"> {summary}")
    
    if lines: lines.append("\n---")

    # 2. 作息分析 (Schedule)
    sch = sections.get('schedule', {})
    if sch.get('status') == 'done' or sch.get('overall'):
        lines.append("\n## 🌙 作息分析")
        if sch.get('overall'):
            lines.append(sch['overall'])
        
        slots = sch.get('slots', [])
        if slots:
            lines.append("")
            for slot in slots:
                r = slot.get('range', '')
                c = slot.get('comment', '')
                if r or c:
                    lines.append(f"- **{r}**: {c}")

    # 3. 活动画像 (Activity)
    act = sections.get('activity', {})
    if act.get('status') == 'done' or act.get('overall'):
        lines.append("\n## 🏃 活动画像")
        if act.get('overall'):
            lines.append(act['overall'])
        
        slots = act.get('slots', [])
        if slots:
            lines.append("")
            for slot in slots:
                r = slot.get('range', '')
                c = slot.get('comment', '')
                if r or c:
                    lines.append(f"- **{r}**: {c}")

    # 4. 有趣发现 (Findings)
    find = sections.get('findings', {})
    if find.get('status') == 'done' or find.get('overall'):
        lines.append("\n## ✨ 有趣发现")
        if find.get('overall'):
            lines.append(find['overall'])
        
        slots = find.get('slots', [])
        if slots:
            lines.append("")
            for slot in slots:
                r = slot.get('range', '')
                c = slot.get('comment', '')
                if r or c:
                    if r: lines.append(f"- **[{r}]** {c}")
                    else: lines.append(f"- {c}")

    # 5. 聊天互动 (Chat)
    chat = sections.get('chat', {})
    if chat.get('status') == 'done' or chat.get('overall'):
        lines.append("\n## 💬 聊天互动")
        if chat.get('overall'):
            lines.append(chat['overall'])
        
        items = chat.get('items', [])
        if items:
            lines.append("")
            for item in items:
                topic = item.get('topic', '')
                comment = item.get('comment', '')
                ref = item.get('ref', '')
                if topic or comment:
                    prefix = f"[{ref}] " if ref else ""
                    lines.append(f"- {prefix}**{topic}**: {comment}")

    # 6. 页脚
    updated_at = analysis_result.get('updated_at')
    if not updated_at:
        # 尝试从模块中找一个最新的更新时间
        updates = [s.get('updated_at') for s in sections.values() if s.get('updated_at')]
        if updates:
            updated_at = max(updates)
            
    if updated_at:
        lines.append(f"\n\n---\n*分析更新于: {updated_at}*")

    return "\n".join(lines).strip()
