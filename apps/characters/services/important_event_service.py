import hashlib
import json
import logging
import math
import re
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.characters.models import DailyReport, ImportantEvent
from .llm_service import extract_text_from_anthropic_response


logger = logging.getLogger(__name__)

DEFAULT_EVENT_LIMIT = 8
DEFAULT_INITIAL_RECALL = 50
DEFAULT_PINNED_LIMIT = 3


EVENT_EXTRACTION_SYSTEM_PROMPT = """你是一个严谨的生活日志事件抽取器。
你的任务是从用户某一天的客观活动聚合数据中，提取值得长期记忆的重要事件。
必须以原始数据为事实依据；日报文字只能作为理解重点的辅助线索。
不要夸大，不要补写不存在的事实，不要把普通的一天硬说成重大事件。"""


EVENT_EXTRACTION_SYSTEM_SUFFIX = """

你现在执行的是“长期重要事件记忆抽取”任务。
请用你的身份和价值判断来决定哪些事件值得未来日报记住，但最终输出必须是中性的结构化 JSON。
不要输出寒暄、解释、Markdown 或角色台词；不要为了符合人设而捏造事实。"""


EVENT_EXTRACTION_USER_PROMPT = """请从下面这一天的数据中抽取 0-6 条“值得长期记忆”的重要事件。

重要事件标准：
1. 对长期理解用户有价值：作息显著异常、持续专注、社交关系/话题变化、健康/运动异常、里程碑、地点/项目/人物反复出现。
2. 必须能从客观数据中找到证据。
3. 普通应用使用、普通步数、没有明显模式的一天，不要强行抽取。
4. 敏感聊天内容要概括，不要复制大段原文。
5. 如果日报中出现对未来日报很有用的 AI 关系、角色设定、系统上线、互动默契、特殊纪念日等信息，也可以抽取为长期事件；这类事件的 evidence 必须说明来自日报辅助摘要或聊天记录。

输出要求：
只输出 JSON 数组，不要 Markdown，不要解释。数组元素字段如下：
[
  {{
    "event_key": "稳定短 key，英文小写/数字/下划线，建议含类型和时间",
    "title": "不超过 30 字的事件标题",
    "summary": "1-2 句客观摘要",
    "event_type": "work_focus|sleep_pattern|social|health|travel|milestone|anomaly|entertainment|ai_relationship|system_milestone|other",
    "time_range": "HH:MM-HH:MM 或空字符串",
    "importance_score": 0-100,
    "confidence": 0-1,
    "entities": ["应用/地点/人物/群名/项目名"],
    "keywords": ["用于检索的关键词"],
    "evidence": ["来自原始数据的短证据，最多 3 条"]
  }}
]

日报文字只作为辅助，不可覆盖原始数据事实。

日期：{date}
客观活动聚合数据 JSON：
{raw_data}

辅助日报摘要：
{daily_markdown}
"""


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _clamp_int(value, default=50, minimum=0, maximum=100):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _clamp_float(value, default=0.8, minimum=0.0, maximum=1.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _as_short_list(value, max_items=8, max_len=120):
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value[:max_items]:
        text = str(item).strip()
        if text:
            cleaned.append(text[:max_len])
    return cleaned


def _safe_event_key(event, report):
    key = str(event.get('event_key') or '').strip().lower()
    key = re.sub(r'[^a-z0-9_\-]+', '_', key).strip('_')[:96]
    if key:
        return key

    digest_source = "|".join([
        str(report.date),
        str(event.get('title') or ''),
        str(event.get('summary') or ''),
        str(event.get('time_range') or ''),
    ])
    return hashlib.sha256(digest_source.encode('utf-8')).hexdigest()[:24]


def _source_hash(report):
    markdown = ''
    if report.analysis_result and isinstance(report.analysis_result, dict):
        markdown = report.analysis_result.get('markdown', '') or ''
    source = {
        'raw_data': report.raw_data or {},
        'markdown': markdown,
    }
    return hashlib.sha256(_json_dumps(source).encode('utf-8')).hexdigest()


def _build_embedding_text(event):
    parts = [
        f"日期: {event.date}",
        f"标题: {event.title}",
        f"摘要: {event.summary}",
        f"类型: {event.event_type}",
        f"时间段: {event.time_range}",
        f"实体: {'、'.join(event.entities or [])}",
        f"关键词: {'、'.join(event.keywords or [])}",
        f"证据: {'；'.join(event.evidence or [])}",
    ]
    return "\n".join(part for part in parts if part and not part.endswith(': '))


def _parse_json_array(text):
    text = (text or '').strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', text, flags=re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, list):
        raise ValueError('LLM output is not a JSON array')
    return value


def _get_anthropic_client():
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    if not api_key:
        return None

    import anthropic

    client_kwargs = {'api_key': api_key}
    base_url = getattr(settings, 'ANTHROPIC_BASE_URL', None)
    if base_url:
        client_kwargs['base_url'] = base_url
    return anthropic.Anthropic(**client_kwargs)


def _build_event_extraction_system_prompt(report):
    try:
        config = report.character.daily_report_config
    except Exception:
        config = None

    ai_persona = config.ai_persona if config else {}
    ai_persona = ai_persona or {}

    core_identity = (ai_persona.get('core_identity') or '').strip()
    personality_traits = (ai_persona.get('personality_traits') or '').strip()
    language_style = (ai_persona.get('language_style') or '').strip()

    if not (core_identity or personality_traits or language_style):
        return EVENT_EXTRACTION_SYSTEM_PROMPT

    parts = []
    if core_identity:
        parts.append(core_identity)
    if personality_traits:
        parts.append(personality_traits)
    if language_style:
        parts.append(f"## 语言风格\n{language_style}")

    return "\n".join(parts) + EVENT_EXTRACTION_SYSTEM_SUFFIX


def extract_important_events_for_report(report, force=False):
    """
    从一份已完成日报中抽取长期重要事件，并同步向量索引。

    返回 dict，包含 created/updated/skipped 等统计。
    """
    if not report.raw_data:
        logger.info("Report %s has no raw_data, skip important event extraction", report.id)
        return {'created': 0, 'updated': 0, 'skipped': True, 'reason': 'empty_raw_data'}

    source_hash = _source_hash(report)
    existing = ImportantEvent.objects.filter(source_report=report, is_active=True)
    if not force and existing.exists() and not existing.exclude(source_hash=source_hash).exists():
        for event in existing.filter(milvus_synced=False):
            sync_event_to_milvus(event)
        return {'created': 0, 'updated': 0, 'skipped': True, 'reason': 'unchanged'}

    client = _get_anthropic_client()
    if client is None:
        logger.warning("Anthropic API key not configured, skip important event extraction")
        return {'created': 0, 'updated': 0, 'skipped': True, 'reason': 'anthropic_not_configured'}

    model = getattr(settings, 'ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')
    markdown = ''
    if report.analysis_result and isinstance(report.analysis_result, dict):
        markdown = (report.analysis_result.get('markdown', '') or '')[:4000]

    prompt = EVENT_EXTRACTION_USER_PROMPT.format(
        date=report.date.isoformat(),
        raw_data=_json_dumps(report.raw_data),
        daily_markdown=markdown or '（无）',
    )

    response = client.messages.create(
        model=model,
        system=_build_event_extraction_system_prompt(report),
        temperature=0.2,
        max_tokens=4096,
        messages=[{'role': 'user', 'content': prompt}],
    )
    result_text = extract_text_from_anthropic_response(response)
    raw_events = _parse_json_array(result_text)

    seen_keys = set()
    created = 0
    updated = 0
    active_event_ids = []

    with transaction.atomic():
        for raw_event in raw_events[:6]:
            if not isinstance(raw_event, dict):
                continue

            title = str(raw_event.get('title') or '').strip()[:120]
            summary = str(raw_event.get('summary') or '').strip()
            if not title or not summary:
                continue

            importance_score = _clamp_int(raw_event.get('importance_score'), default=60)
            confidence = _clamp_float(raw_event.get('confidence'), default=0.8)
            if importance_score < 55 or confidence < 0.45:
                continue

            event_key = _safe_event_key(raw_event, report)
            if event_key in seen_keys:
                continue
            seen_keys.add(event_key)

            defaults = {
                'source_report': report,
                'title': title,
                'summary': summary[:1000],
                'event_type': str(raw_event.get('event_type') or 'other').strip()[:50],
                'time_range': str(raw_event.get('time_range') or '').strip()[:50],
                'importance_score': importance_score,
                'confidence': confidence,
                'entities': _as_short_list(raw_event.get('entities'), max_items=10, max_len=80),
                'keywords': _as_short_list(raw_event.get('keywords'), max_items=12, max_len=50),
                'evidence': _as_short_list(raw_event.get('evidence'), max_items=3, max_len=160),
                'source_hash': source_hash,
                'is_active': True,
                'milvus_synced': False,
                'milvus_synced_at': None,
            }

            event, was_created = ImportantEvent.objects.update_or_create(
                character=report.character,
                date=report.date,
                event_key=event_key,
                defaults=defaults,
            )
            event.embedding_text = _build_embedding_text(event)
            event.save(update_fields=['embedding_text'])
            active_event_ids.append(event.id)
            created += 1 if was_created else 0
            updated += 0 if was_created else 1

        stale_events = ImportantEvent.objects.filter(source_report=report, is_active=True).exclude(id__in=active_event_ids)
        stale_ids = list(stale_events.values_list('id', flat=True))
        stale_events.update(is_active=False, milvus_synced=False, milvus_synced_at=None)

    for event_id in stale_ids:
        delete_event_from_milvus(event_id)

    for event in ImportantEvent.objects.filter(id__in=active_event_ids):
        sync_event_to_milvus(event)

    return {'created': created, 'updated': updated, 'skipped': False}


def _ollama_embed(text):
    base_url = getattr(settings, 'OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
    model = getattr(settings, 'OLLAMA_EMBED_MODEL', 'mxbai-embed-large')
    payload = json.dumps({'model': model, 'input': text}, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        f'{base_url}/api/embed',
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Ollama embedding request failed: %s", exc)
        return None

    embeddings = data.get('embeddings')
    if embeddings and isinstance(embeddings, list):
        embedding = embeddings[0]
    else:
        embedding = data.get('embedding')

    if not isinstance(embedding, list):
        return None
    return [float(x) for x in embedding]


def _get_milvus_collection():
    try:
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility
    except ImportError:
        logger.info("pymilvus is not installed; vector memory index is disabled")
        return None

    collection_name = getattr(settings, 'MILVUS_IMPORTANT_EVENT_COLLECTION', 'important_events')
    alias = 'important_event_memory'
    uri = getattr(settings, 'MILVUS_URI', '')
    token = getattr(settings, 'MILVUS_TOKEN', '')
    db_name = getattr(settings, 'MILVUS_DB_NAME', '')

    try:
        if not connections.has_connection(alias):
            if uri:
                kwargs = {'alias': alias, 'uri': uri}
                if token:
                    kwargs['token'] = token
                if db_name:
                    kwargs['db_name'] = db_name
                connections.connect(**kwargs)
            else:
                kwargs = {
                    'alias': alias,
                    'host': getattr(settings, 'MILVUS_HOST', '127.0.0.1'),
                    'port': str(getattr(settings, 'MILVUS_PORT', '19530')),
                }
                if db_name:
                    kwargs['db_name'] = db_name
                connections.connect(**kwargs)

        if not utility.has_collection(collection_name, using=alias):
            dim = int(getattr(settings, 'OLLAMA_EMBED_DIM', 1024))
            fields = [
                FieldSchema(name='event_id', dtype=DataType.VARCHAR, max_length=32, is_primary=True),
                FieldSchema(name='character_uid', dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name='event_date', dtype=DataType.VARCHAR, max_length=10),
                FieldSchema(name='importance_score', dtype=DataType.INT64),
                FieldSchema(name='embedding', dtype=DataType.FLOAT_VECTOR, dim=dim),
            ]
            schema = CollectionSchema(fields=fields, description='StillAlive important event memory')
            collection = Collection(collection_name, schema=schema, using=alias)
            collection.create_index(
                field_name='embedding',
                index_params={'metric_type': 'COSINE', 'index_type': 'HNSW', 'params': {'M': 16, 'efConstruction': 200}},
            )
        else:
            collection = Collection(collection_name, using=alias)

        collection.load()
        return collection
    except Exception as exc:
        logger.warning("Milvus collection unavailable: %s", exc)
        return None


def sync_event_to_milvus(event):
    if not getattr(settings, 'IMPORTANT_EVENT_VECTOR_ENABLED', True):
        return False

    embedding = _ollama_embed(event.embedding_text or _build_embedding_text(event))
    if not embedding:
        return False

    collection = _get_milvus_collection()
    if collection is None:
        return False

    try:
        expr = f'event_id == "{event.id}"'
        collection.delete(expr)
        collection.insert([
            [str(event.id)],
            [str(event.character_id)],
            [event.date.isoformat()],
            [int(event.importance_score)],
            [embedding],
        ])
        collection.flush()
        event.milvus_synced = True
        event.milvus_synced_at = timezone.now()
        event.save(update_fields=['milvus_synced', 'milvus_synced_at'])
        return True
    except Exception as exc:
        logger.warning("Failed to sync important event %s to Milvus: %s", event.id, exc)
        return False


def delete_event_from_milvus(event_id):
    collection = _get_milvus_collection()
    if collection is None:
        return False
    try:
        collection.delete(f'event_id == "{event_id}"')
        collection.flush()
        return True
    except Exception as exc:
        logger.warning("Failed to delete important event %s from Milvus: %s", event_id, exc)
        return False


def _build_retrieval_query(aggregated_data):
    parts = [
        f"日期: {aggregated_data.get('date', '')}",
        f"活动时间段: {'、'.join(aggregated_data.get('active_time_ranges') or [])}",
    ]
    for key, label in [
        ('phone_app_summary', '手机应用'),
        ('computer_app_summary', '电脑应用'),
        ('steps_summary', '步数'),
        ('qq_messages_summary', '聊天摘要'),
    ]:
        value = aggregated_data.get(key)
        if value:
            parts.append(f"{label}: {_json_dumps(value)[:1200]}")

    qq_messages = aggregated_data.get('qq_messages') or []
    if qq_messages:
        parts.append(f"聊天详情: {_json_dumps(qq_messages)[:1800]}")

    return "\n".join(parts)


def _search_milvus_event_ids(character, query_text, recall):
    embedding = _ollama_embed(query_text)
    if not embedding:
        return {}

    collection = _get_milvus_collection()
    if collection is None:
        return {}

    try:
        results = collection.search(
            data=[embedding],
            anns_field='embedding',
            param={'metric_type': 'COSINE', 'params': {'ef': max(64, recall)}},
            limit=recall,
            expr=f'character_uid == "{character.uid}"',
            output_fields=['event_id'],
        )
    except Exception as exc:
        logger.warning("Milvus important event search failed: %s", exc)
        return {}

    scores = {}
    for hit in results[0]:
        event_id = hit.entity.get('event_id') if hit.entity else hit.id
        scores[int(event_id)] = float(hit.distance)
    return scores


def _recency_score(event, target_date):
    if not target_date:
        return 0.5
    age_days = max(0, (target_date - event.date).days)
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.75
    if age_days <= 90:
        return 0.45
    return max(0.15, math.exp(-age_days / 180))


def _metadata_match_score(event, query_text):
    query_text = query_text.lower()
    terms = [str(x).lower() for x in (event.entities or []) + (event.keywords or []) if str(x).strip()]
    if not terms:
        return 0.0
    matched = sum(1 for term in terms if term and term in query_text)
    return min(1.0, matched / min(5, len(terms)))


def retrieve_important_events(character, aggregated_data, limit=DEFAULT_EVENT_LIMIT):
    if not getattr(settings, 'IMPORTANT_EVENT_MEMORY_ENABLED', True):
        return []

    target_date = None
    if aggregated_data.get('date'):
        try:
            target_date = timezone.datetime.fromisoformat(aggregated_data['date']).date()
        except Exception:
            target_date = None

    query_text = _build_retrieval_query(aggregated_data)
    recall = int(getattr(settings, 'IMPORTANT_EVENT_INITIAL_RECALL', DEFAULT_INITIAL_RECALL))
    vector_scores = _search_milvus_event_ids(character, query_text, recall)

    candidate_ids = set(vector_scores.keys())
    cutoff_date = (target_date - timedelta(days=180)) if target_date else None
    fallback_qs = ImportantEvent.objects.filter(character=character, is_active=True)
    if target_date:
        fallback_qs = fallback_qs.filter(date__lt=target_date)
    if cutoff_date:
        fallback_qs = fallback_qs.filter(date__gte=cutoff_date)

    fallback_ids = list(
        fallback_qs.order_by('-importance_score', '-date').values_list('id', flat=True)[: max(limit * 3, 20)]
    )
    candidate_ids.update(fallback_ids)

    pinned_limit = int(getattr(settings, 'IMPORTANT_EVENT_PINNED_LIMIT', DEFAULT_PINNED_LIMIT))
    pinned_ids = list(
        fallback_qs.filter(importance_score__gte=90)
        .order_by('-importance_score', '-date')
        .values_list('id', flat=True)[:pinned_limit]
    )
    candidate_ids.update(pinned_ids)

    if not candidate_ids:
        return []

    events = list(ImportantEvent.objects.filter(id__in=candidate_ids, is_active=True))
    scored = []
    for event in events:
        vector_score = vector_scores.get(event.id, 0.0)
        final_score = (
            0.45 * max(0.0, min(1.0, vector_score))
            + 0.25 * (event.importance_score / 100)
            + 0.15 * _recency_score(event, target_date)
            + 0.10 * _metadata_match_score(event, query_text)
            + 0.05 * max(0.0, min(1.0, event.confidence))
        )
        scored.append((final_score, event))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [event for _, event in scored[:limit]]


def format_events_for_prompt(events):
    if not events:
        return ''

    lines = ["\n## 可能相关的长期重要记忆"]
    for index, event in enumerate(events, start=1):
        detail = f"{index}. {event.date} {event.title}：{event.summary}"
        meta = []
        if event.event_type:
            meta.append(f"类型 {event.event_type}")
        meta.append(f"重要度 {event.importance_score}")
        if event.time_range:
            meta.append(f"时间 {event.time_range}")
        if event.evidence:
            meta.append(f"证据 {'；'.join(event.evidence[:2])}")
        lines.append(f"{detail}（{'，'.join(meta)}）")

    lines.append("\n请把这些记忆当作背景线索自然使用；如果今天数据不相关，不要强行提及。")
    return "\n".join(lines)


def check_important_event_infra():
    """
    启动期基础设施自检。

    只输出日志，不抛异常；避免 Web 服务因为向量依赖暂时不可用而启动失败。
    """
    result = {
        'memory_enabled': getattr(settings, 'IMPORTANT_EVENT_MEMORY_ENABLED', True),
        'vector_enabled': getattr(settings, 'IMPORTANT_EVENT_VECTOR_ENABLED', True),
        'ollama_ok': None,
        'milvus_ok': None,
    }

    if not result['memory_enabled']:
        logger.info("Important event memory is disabled")
        return result

    if not result['vector_enabled']:
        logger.info("Important event vector index is disabled; DB fallback retrieval remains available")
        return result

    embedding = _ollama_embed("StillAlive important event memory infrastructure check")
    result['ollama_ok'] = bool(embedding)
    if result['ollama_ok']:
        logger.info(
            "Important event memory Ollama check OK: model=%s dim=%s base_url=%s",
            getattr(settings, 'OLLAMA_EMBED_MODEL', 'mxbai-embed-large'),
            len(embedding),
            getattr(settings, 'OLLAMA_BASE_URL', ''),
        )
    else:
        logger.warning(
            "Important event memory Ollama check failed: base_url=%s model=%s",
            getattr(settings, 'OLLAMA_BASE_URL', ''),
            getattr(settings, 'OLLAMA_EMBED_MODEL', ''),
        )

    collection = _get_milvus_collection()
    result['milvus_ok'] = collection is not None
    if result['milvus_ok']:
        logger.info(
            "Important event memory Milvus check OK: collection=%s",
            getattr(settings, 'MILVUS_IMPORTANT_EVENT_COLLECTION', 'important_events'),
        )
    else:
        logger.warning(
            "Important event memory Milvus check failed: host=%s port=%s uri=%s db_name=%s collection=%s",
            getattr(settings, 'MILVUS_HOST', ''),
            getattr(settings, 'MILVUS_PORT', ''),
            getattr(settings, 'MILVUS_URI', ''),
            getattr(settings, 'MILVUS_DB_NAME', ''),
            getattr(settings, 'MILVUS_IMPORTANT_EVENT_COLLECTION', ''),
        )

    return result
