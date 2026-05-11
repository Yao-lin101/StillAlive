import json
import logging
import re
from django.utils import timezone
from .data_service import ACTIVE_INTERVAL_MAX_GAP

import time
from django.conf import settings

logger = logging.getLogger(__name__)

# ── 基础工具函数 (Basic Utilities) ──────────────────────────────────

def extract_text_from_response(response):
    """
    安全地从 Anthropic API 的 response 中提取文本，兼容带有 thinking block 的模型。
    """
    result_text = None
    
    if hasattr(response, 'content'):
        text_blocks = []
        for block in response.content:
            block_type = getattr(block, 'type', 'unknown')
            
            if block_type == 'text':
                if hasattr(block, 'text') and block.text is not None:
                    text_blocks.append(block.text)
            
            # 兼容带有 thinking block 的响应
            if block_type == 'thinking':
                pass # 暂不提取推理过程
                    
        if text_blocks:
            # 代理 API 可能会将推理过程作为第一个 text block，将最终回复作为最后一个 text block
            result_text = text_blocks[-1]
        
        if result_text is None:
            try:
                # 最后的兜底尝试
                str_content = str(response.content[-1])
                match = re.search(r"text='([^']+)'", str_content)
                if match:
                    result_text = match.group(1)
            except Exception:
                pass
                
    if not result_text and hasattr(response, 'text'):
        result_text = response.text
        
    # 过滤可能存在的内置 <think> 标签
    if result_text:
        result_text = re.sub(r'<think>.*?</think>', '', result_text, flags=re.DOTALL).strip()
        
    return result_text


def call_anthropic_api(system_prompt, user_prompt, max_tokens=8192, temperature=0.7, max_retries=3):
    """
    统一的 Anthropic API 调用入口，包含重试和超时逻辑。
    """
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    if not api_key:
        return {"error": "API Key not configured"}

    import anthropic
    base_url = getattr(settings, 'ANTHROPIC_BASE_URL', None)
    model = getattr(settings, 'ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')
    
    # 建立客户端，显式设置超时
    client = anthropic.Anthropic(
        api_key=api_key, 
        base_url=base_url,
        timeout=180.0 # 统一 180s 超时
    ) if base_url else anthropic.Anthropic(api_key=api_key, timeout=180.0)

    retry_count = 0
    backoff_delay = 2 # 初始退避 2 秒

    while retry_count <= max_retries:
        try:
            response = client.messages.create(
                model=model,
                system=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[{"role": "user", "content": user_prompt}]
            )
            return response
        except Exception as e:
            retry_count += 1
            if retry_count > max_retries:
                logger.error(f"LLM API call failed after {max_retries} retries: {str(e)}")
                raise e
            
            # 只有特定的错误才重试（超时、500、频率限制等）
            error_str = str(e).lower()
            is_retryable = any(kw in error_str for kw in ['timeout', '500', '502', '503', '504', 'overloaded', 'rate_limit'])
            
            if is_retryable:
                logger.warning(f"LLM API call error: {str(e)}. Retrying in {backoff_delay}s... (Attempt {retry_count}/{max_retries})")
                time.sleep(backoff_delay)
                backoff_delay *= 2 # 指数退避
            else:
                # 非可重试错误（如 400 格式错误）直接抛出
                logger.error(f"LLM API non-retryable error: {str(e)}")
                raise e


def clean_markdown_wrapper(text):
    """
    清理 LLM 回复格式中多余的 markdown 代码块包裹
    """
    if not text: return ""
    text = text.strip()
    
    # 情况 1：被 ```markdown 或 ```json 或 ``` 包裹
    patterns = [
        r'^```markdown\s*\n(.*?)\n```\s*$',
        r'^```json\s*\n(.*?)\n```\s*$',
        r'^```\s*\n(.*?)\n```\s*$'
    ]
    
    for p in patterns:
        match = re.match(p, text, re.DOTALL)
        if match:
            return match.group(1).strip()
    
    # 情况 2：不完整的代码块（只有开头没有结尾，常见于截断）
    if text.startswith('```'):
        text = re.sub(r'^```(markdown|json|)\s*', '', text).strip()
    if text.endswith('```'):
        text = text[:-3].strip()
        
    return text


def safe_json_loads(text):
    """
    极其鲁棒的 JSON 解析器
    1. 寻找最外层的大括号
    2. 处理转义字符
    3. 尝试多种解析策略
    """
    if not text: return None
    
    # 预处理：初步清理
    text = clean_markdown_wrapper(text)
    
    # 策略 1：精准截取 JSON 核心部分
    try:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            json_str = text[start:end+1]
            return json.loads(json_str)
    except Exception:
        pass

    # 策略 2：处理常见的转义符问题
    try:
        # 修复某些 LLM 返回的错误转义
        processed = text.replace('\\', '\\\\').replace('\\\\"', '\\"')
        json_match = re.search(r'(\{.*\}|\[.*\])', processed, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))
    except Exception:
        pass
        
    # 策略 3：直接解析
    try:
        return json.loads(text)
    except Exception as e:
        logger.error(f"JSON final parse error: {str(e)}\nRaw text snippets: {text[:100]}...")
        return None

# ── 数据格式化工具 (Data Formatting) ────────────────────────────────

def format_usage_overview(data_summary, target_date_str, cutoff_time_str):
    """基础概览信息"""
    return f"""## 数据概览
- 日期: {target_date_str}
- 总记录数: {data_summary.get('total_records', 0)}
- 数据截止时间: {cutoff_time_str}
"""

def format_active_ranges(data_summary):
    """活跃周期格式化"""
    global_ranges = data_summary.get('global_active_time_ranges', [])
    if not global_ranges:
        return ""
        
    ranges_str = "\n".join([f"- {r}" for r in global_ranges])
    return f"""
## 近期连续活跃周期
（注：已自动合并跨天活动，相差{ACTIVE_INTERVAL_MAX_GAP}分钟以内的活动将被连接。两个周期之间的空白代表脱机或休息。）
{ranges_str}
"""

def format_app_usage(data_summary, platform='phone'):
    """应用使用摘要"""
    key = f'{platform}_app_summary'
    by_time_key = f'{platform}_app_by_time_range'
    title = "手机应用" if platform == 'phone' else "电脑应用"
    
    content = ""
    summary = data_summary.get(key)
    if summary:
        content += f"\n## {title}（总计前20）\n{json.dumps(summary, ensure_ascii=False)}\n"
        
    # 详细时间轴（非紧凑模式下可用）
    detail = data_summary.get(by_time_key)
    if detail:
        content += f"\n## {title}（按时间范围）\n{json.dumps(detail, ensure_ascii=False)}\n"
        
    return content

def format_steps_info(data_summary):
    """步数信息"""
    steps = data_summary.get('steps_summary', {})
    total = steps.get('total', 0)
    if total == 0: return ""
    
    content = f"\n## 今日总步数: {total}\n"
    by_hour = data_summary.get('steps_by_hour')
    if by_hour:
        content += f"\n## 步数（按小时累计）\n{json.dumps(by_hour, ensure_ascii=False)}\n"
    return content

def format_chat_logs(data_summary):
    """
    QQ 聊天记录格式化（包含私聊和群聊）
    """
    qq_messages = data_summary.get('qq_messages', [])
    if not qq_messages: return ""
    
    private_list = []
    group_list = []
    for block in qq_messages:
        m_type = block.get('message_type')
        m_data = block.get('message_data', [])
        if m_type == 'private':
            private_list.extend(m_data)
        elif m_type == 'group':
            group_list.extend(m_data)

    content = ""
    
    # 1. 私聊处理
    if private_list:
        content += "\n## 私人聊天内容\n"
        for item in private_list:
            time_str = item.get('时间') or '未知'
            content += f"[{time_str}]\n"
            if '话题' in item: content += f"话题: {item['话题']}\n"
            if '总结' in item: content += f"总结: {item['总结']}\n"
            
            user_msg = item.get('用户')
            bot_msg = item.get('你的回复')
            if user_msg: content += f"用户: {user_msg}\n"
            if bot_msg:
                # 截断超长回复
                if len(bot_msg) > 100:
                    bot_msg = bot_msg.split('\n')[0] + '...'
                content += f"你: {bot_msg}\n"
            content += "\n"

    # 2. 群聊处理
    if group_list:
        content += "\n## QQ群聊内容总结\n"
        groups = {}
        for block in group_list:
            g_name = block.get('群名称', '未知群聊')
            if g_name not in groups:
                groups[g_name] = {
                    'bot': block.get('你在本群昵称', '未知'),
                    'user': block.get('用户在本群昵称', '未知'),
                    'topics': []
                }
            groups[g_name]['topics'].append({
                'time': block.get('时间', '未知'),
                'summary': block.get('话题总结', '')
            })
            
        for name, info in groups.items():
            content += f"### 【{name}】\n"
            content += f"- 你的群昵称: {info['bot']}\n"
            content += f"- 用户的群昵称: {info['user']}\n\n"
            for t in info['topics']:
                content += f"#### [{t['time']}]\n{t['summary']}\n\n"
                
    return content

# ── 综合业务工具 (Business Logic Utilities) ────────────────────────

def build_data_section(data_summary, target_date_str, cutoff_time_str, **options):
    """
    模块化构建数据快照文本
    options: exclude_apps, exclude_steps, exclude_active_ranges, compact_mode
    """
    compact = options.get('compact_mode', False)
    exclude_apps = options.get('exclude_apps', False)
    exclude_steps = options.get('exclude_steps', False)
    exclude_active_ranges = options.get('exclude_active_ranges', False)
    
    sections = []
    
    # 1. 基础
    sections.append(format_usage_overview(data_summary, target_date_str, cutoff_time_str))
    
    # 2. 活跃周期
    if not exclude_active_ranges and not compact:
        sections.append(format_active_ranges(data_summary))
        
    # 3. 应用使用
    if not exclude_apps:
        # 如果是紧凑模式，不输出时间轴详情，手动控制 data_summary 的键
        temp_summary = data_summary.copy()
        if compact:
            temp_summary.pop('phone_app_by_time_range', None)
            temp_summary.pop('computer_app_by_time_range', None)
            
        sections.append(format_app_usage(temp_summary, 'phone'))
        sections.append(format_app_usage(temp_summary, 'computer'))
        
    # 4. 步数
    if not exclude_steps:
        sections.append(format_steps_info(data_summary))
        
    # 5. 聊天记录
    sections.append(format_chat_logs(data_summary))
    
    return "\n".join(filter(None, sections))


def get_redaction_keys(data_summary):
    """
    从数据摘要中提取所有可能的敏感键值（应用名、标题等）
    """
    keys = set()
    
    # 提取应用名
    for p in ['phone', 'computer']:
        summary = data_summary.get(f'{p}_app_summary', {})
        if isinstance(summary, dict): keys.update(summary.keys())
        
        detail = data_summary.get(f'{p}_app_by_time_range', {})
        if isinstance(detail, dict):
            for apps in detail.values():
                if isinstance(apps, dict): keys.update(apps.keys())
                
    return [k for k in keys if k and k.strip()]


def apply_redactions(text, redaction_items):
    """
    执行数据脱敏替换
    """
    if not redaction_items or not text:
        return text
        
    mask_text = "【隐藏剧情】"
    result = text
    
    # 兼容列表和字典格式
    if isinstance(redaction_items, list):
        for original in redaction_items:
            if original and original.strip():
                result = result.replace(original, mask_text)
    elif isinstance(redaction_items, dict):
        for original, replacement in redaction_items.items():
            if original and original.strip():
                result = result.replace(original, replacement)
                
    return result
