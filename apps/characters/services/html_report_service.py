"""
HTML报告结构化数据生成服务

将 DailyReport.raw_data 转换为前端HTML模板所需的结构化数据：
- 图表数据（步数、活动时段、App使用情况）
- LLM评论占位（从 analysis_result 中提取，或置空）
"""
import logging
from collections import defaultdict

import re

logger = logging.getLogger(__name__)

def _clean_app_name(name):
    """清洗应用名称，合并浏览器标题等冗余信息（仅用于前端展示脱敏）"""
    if not name:
        return name
    # 兼容英文半角和中文全角冒号，清洗并聚合冒号前的应用名
    for char in (':', '：'):
        if char in name:
            return name.split(char, 1)[0].strip()
    return name.strip()


# ──────────────────────────────────────────────
# 步数图表数据处理
# ──────────────────────────────────────────────

def _build_steps_chart(steps_by_hour: dict) -> dict:
    """
    将 steps_by_hour 转换为图表可用的格式。
    确保返回 24 个数据点，对齐 00:00 - 23:00。
    """
    if not steps_by_hour:
        return {
            "labels": [f"{h:02d}:00" for h in range(24)],
            "values": [0] * 24,
            "total": 0,
            "max_value": 0
        }

    hourly = {int(k): v for k, v in steps_by_hour.items()}
    
    labels = []
    values = []
    prev_total = 0
    
    # 遍历 24 小时
    for h in range(24):
        labels.append(f"{h:02d}:00")
        if h in hourly:
            current_cumulative = hourly[h]
            delta = max(0, current_cumulative - prev_total)
            values.append(delta)
            prev_total = current_cumulative
        else:
            # 如果这一小时没记录，步数增量为 0，但 prev_total 不变
            values.append(0)

    total = max(hourly.values()) if hourly else 0
    return {
        "labels": labels,
        "values": values,
        "total": total,
        "max_value": max(values) if values else 0,
    }


# ──────────────────────────────────────────────
# 活动时段图表数据处理
# ──────────────────────────────────────────────

def _parse_time_to_minutes(time_str: str) -> int:
    """将 'HH:MM' 格式转为分钟数（从0点起）"""
    try:
        parts = time_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return 0


def _build_activity_timeline(raw_data: dict) -> dict:
    """
    生成24小时活跃时段热力图数据。
    返回每小时活跃度（0=不活跃，1=轻度活跃，2=高度活跃）。
    使用 phone_app_by_time_range 和 computer_app_by_time_range 来计算。
    """
    # 每小时活跃分钟数统计
    hour_active_minutes = defaultdict(float)

    def process_time_range(by_time_range: dict):
        if not isinstance(by_time_range, dict):
            return
        for time_range, apps in by_time_range.items():
            if "-" not in time_range:
                continue
            parts = time_range.split("-")
            if len(parts) != 2:
                continue
            start_min = _parse_time_to_minutes(parts[0])
            end_min = _parse_time_to_minutes(parts[1])
            if end_min <= start_min:
                end_min += 24 * 60  # 跨天处理（极少见）
            
            duration = end_min - start_min
            if duration <= 0 or duration > 120:  # 超过2小时的异常窗口忽略
                continue

            # 将活跃时长分配到对应小时
            for h in range(24):
                h_start = h * 60
                h_end = h_start + 60
                overlap_start = max(start_min, h_start)
                overlap_end = min(end_min, h_end)
                if overlap_end > overlap_start:
                    hour_active_minutes[h] += (overlap_end - overlap_start)

    process_time_range(raw_data.get("phone_app_by_time_range", {}))
    process_time_range(raw_data.get("computer_app_by_time_range", {}))
    process_time_range(raw_data.get("computer_app_2_by_time_range", {}))

    # 构建24小时数据
    hours = []
    for h in range(24):
        active_min = hour_active_minutes.get(h, 0)
        if active_min >= 30:
            level = 2  # 高度活跃
        elif active_min >= 5:
            level = 1  # 轻度活跃
        else:
            level = 0  # 不活跃
        hours.append({
            "hour": h,
            "label": f"{h:02d}",
            "level": level,
            "active_minutes": round(active_min, 1),
        })

    # 全局活跃时间段（用于文字展示）
    global_ranges = raw_data.get("global_active_time_ranges", [])
    today_ranges = raw_data.get("active_time_ranges", [])

    return {
        "hours": hours,
        "global_ranges": global_ranges,
        "today_ranges": today_ranges,
    }


# ──────────────────────────────────────────────
# App使用情况图表数据处理
# ──────────────────────────────────────────────

def _build_app_usage_chart(raw_data: dict) -> dict:
    """
    合并手机和电脑的App使用情况，生成饼图/横条图数据。
    phone_app_summary 和 computer_app_summary 格式：{"App名": 次数, ...}
    """
    phone_apps = raw_data.get("phone_app_summary", {}) or {}
    computer_apps = raw_data.get("computer_app_summary", {}) or {}
    computer_2_apps = raw_data.get("computer_app_2_summary", {}) or {}

    # 构建分设备数据（取Top8）
    def top_apps(app_dict: dict, limit: int = 8) -> list:
        if not isinstance(app_dict, dict):
            return []
        sorted_apps = sorted(app_dict.items(), key=lambda x: x[1], reverse=True)
        return [{"name": k, "count": v} for k, v in sorted_apps[:limit]]

    # 合并计算总使用频次（用于展示总览）并进行清洗
    merged = defaultdict(int)
    for k, v in phone_apps.items():
        clean_k = _clean_app_name(k)
        merged[clean_k] += v
    for k, v in computer_apps.items():
        clean_k = _clean_app_name(k)
        merged[clean_k] += v
    for k, v in computer_2_apps.items():
        clean_k = _clean_app_name(k)
        merged[clean_k] += v

    # 同时也清洗分设备的数据
    def process_app_dict(app_dict: dict) -> dict:
        if not isinstance(app_dict, dict): return {}
        cleaned = defaultdict(int)
        for k, v in app_dict.items():
            cleaned[_clean_app_name(k)] += v
        return dict(cleaned)

    phone_cleaned = process_app_dict(phone_apps)
    computer_cleaned = process_app_dict(computer_apps)
    computer_2_cleaned = process_app_dict(computer_2_apps)

    phone_top = top_apps(phone_cleaned)
    computer_top = top_apps(computer_cleaned)
    computer_2_top = top_apps(computer_2_cleaned)

    all_top = sorted(merged.items(), key=lambda x: x[1], reverse=True)[:10]
    combined = [{"name": k, "count": v} for k, v in all_top]

    total_phone = sum(phone_apps.values()) if phone_apps else 0
    total_computer = sum(computer_apps.values()) if computer_apps else 0
    total_computer_2 = sum(computer_2_apps.values()) if computer_2_apps else 0

    return {
        "phone": phone_top,
        "computer": computer_top,
        "computer_2": computer_2_top,
        "combined": combined,
        "total_phone_records": total_phone,
        "total_computer_records": total_computer + total_computer_2,
        "has_phone": bool(phone_top),
        "has_computer": bool(computer_top) or bool(computer_2_top),
        "computer_key": raw_data.get("computer_key"),
        "computer_key_2": raw_data.get("computer_key_2"),
    }


# ──────────────────────────────────────────────
# 群聊数据处理
# ──────────────────────────────────────────────

def _build_chat_summary(raw_data: dict) -> dict:
    """
    提取群聊和私聊摘要数据，供LLM评论模块使用。
    """
    qq_messages = raw_data.get("qq_messages", [])
    qq_summary = raw_data.get("qq_messages_summary", {}) or {}

    private_topics = []
    group_chats = {}

    for msg_record in qq_messages:
        if not isinstance(msg_record, dict):
            continue
        msg_type = msg_record.get("message_type")
        message_data = msg_record.get("message_data", [])

        if msg_type == "private":
            for block in message_data:
                if isinstance(block, dict):
                    topic = block.get("话题", "")
                    summary = block.get("总结", "")
                    time_str = block.get("时间", "")
                    if topic or summary:
                        private_topics.append({
                            "time": time_str,
                            "topic": topic,
                            "summary": summary,
                        })

        elif msg_type == "group":
            for block in message_data:
                if isinstance(block, dict):
                    group_name = block.get("群名称", "未知群聊")
                    if group_name not in group_chats:
                        group_chats[group_name] = {
                            "name": group_name,
                            "bot_nickname": block.get("你在本群昵称", ""),
                            "user_nickname": block.get("用户在本群昵称", ""),
                            "topics": [],
                        }
                    topic_summary = block.get("话题总结", "")
                    time_str = block.get("时间", "")
                    if topic_summary:
                        group_chats[group_name]["topics"].append({
                            "time": time_str,
                            "summary": topic_summary,
                        })

    return {
        "has_private": bool(private_topics),
        "has_group": bool(group_chats),
        "private_topics": private_topics,
        "group_chats": list(group_chats.values()),
        "total_message_blocks": qq_summary.get("total_message_blocks", 0),
    }


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

def build_report_data(raw_data: dict, analysis_result: dict, field_mappings: dict = None) -> dict:
    """
    生成结构化HTML报告数据（图表数据 + LLM评论）
    """
    if not raw_data:
        return {}

    try:
        steps_by_hour = raw_data.get("steps_by_hour", {}) or {}
        steps_summary = raw_data.get("steps_summary", {}) or {}

        steps_chart = _build_steps_chart(steps_by_hour)
        # 用 steps_summary.total 覆盖累计值，因为它更准确
        if steps_summary.get("total"):
            steps_chart["total"] = steps_summary["total"]

        activity_timeline = _build_activity_timeline(raw_data)
        app_usage = _build_app_usage_chart(raw_data)
        
        # 注入用户配置的键名 (优先从字段映射中读取最新的，否则从持久化的 raw_data 中读取)
        if field_mappings:
            app_usage["computer_key"] = field_mappings.get("computer_app")
            app_usage["computer_key_2"] = field_mappings.get("computer_app_2")
        elif raw_data:
            app_usage["computer_key"] = raw_data.get("computer_key")
            app_usage["computer_key_2"] = raw_data.get("computer_key_2")
        chat_data = _build_chat_summary(raw_data)

        # LLM 评论（如果 analysis_result 中有 sections，就用它；否则用 markdown）
        # 目前先从 markdown 中提取，未来改为结构化 JSON
        llm_comments = _extract_llm_comments(analysis_result)

        return {
            "meta": {
                "date": raw_data.get("date", ""),
                "total_records": raw_data.get("total_records", 0),
                "data_cutoff_time": raw_data.get("data_cutoff_time", ""),
            },
            "steps": steps_chart,
            "activity": activity_timeline,
            "apps": app_usage,
            "chat": chat_data,
            "llm": llm_comments,
        }
    except Exception as e:
        logger.error(f"Failed to build report data: {e}", exc_info=True)
        return {}


def _extract_llm_comments(analysis_result: dict) -> dict:
    """
    从 analysis_result 中提取LLM评论。
    支持 V1 (markdown) 和 V2 (structured sections)。
    """
    default_comments = {
        "overall": None,
        "schedule": None,
        "activity": None,
        "findings": None,
        "chat": None,
        "title": None,
        "has_content": False,
        "version": 1,
        "sections": {}
    }

    if not analysis_result or not isinstance(analysis_result, dict):
        return default_comments

    version = analysis_result.get("version", 1)
    markdown = analysis_result.get("markdown", "") or ""
    sections = analysis_result.get("sections", {})

    if version >= 2 and sections:
        # V2 结构化提取
        all_modules = ['title_summary', 'schedule', 'activity', 'findings', 'chat']
        sections_status = {}
        for m in all_modules:
            if m in sections:
                sections_status[m] = sections[m].get("status", "done")
            else:
                sections_status[m] = "pending"

        return {
            "version": version,
            "title": sections.get("title_summary", {}).get("title"),
            "overall": sections.get("title_summary", {}).get("summary"),
            "schedule": sections.get("schedule", {}).get("overall"),
            "schedule_slots": sections.get("schedule", {}).get("slots", []),
            "activity": sections.get("activity", {}).get("overall"),
            "activity_slots": sections.get("activity", {}).get("slots", []),
            "findings": sections.get("findings", {}).get("overall"),
            "findings_slots": sections.get("findings", {}).get("slots", []),
            "chat": sections.get("chat", {}).get("overall"),
            "chat_items": sections.get("chat", {}).get("items", []),
            "has_content": True,
            "sections_status": sections_status,
            "raw_markdown": markdown,
        }
    else:
        # V1 回退逻辑
        has_content = bool(markdown.strip())
        return {
            "version": 1,
            "overall": markdown if has_content else None,
            "schedule": None,
            "activity": None,
            "findings": None,
            "chat": None,
            "title": None,
            "has_content": has_content,
            "raw_markdown": markdown,
        }
