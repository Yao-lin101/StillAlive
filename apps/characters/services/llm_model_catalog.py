"""从供应商读取临时模型目录，不修改数据库或调用链。"""

import logging

from django.core.exceptions import ValidationError

from apps.characters.models import LLMProvider

logger = logging.getLogger(__name__)


class LLMModelDiscoveryError(ValueError):
    pass


def _page_models(first_page):
    for page in first_page.iter_pages():
        items = getattr(page, 'data', None)
        if not isinstance(items, list):
            raise LLMModelDiscoveryError('供应商返回了无效的模型列表，请检查 Base URL 和接口协议。')
        yield from items


def _normalize_models(items, *, gemini=False):
    models = {}
    for item in items:
        if gemini:
            actions = getattr(item, 'supported_actions', None)
            if actions and 'generateContent' not in actions:
                continue
        model_id = getattr(item, 'name' if gemini else 'id', None)
        if not isinstance(model_id, str) or not model_id.strip():
            raise LLMModelDiscoveryError('供应商返回了无效的模型 ID，请检查模型列表接口。')
        model_id = model_id.strip()
        if gemini:
            model_id = model_id.removeprefix('models/')
            if not model_id:
                raise LLMModelDiscoveryError('供应商返回了无效的模型 ID，请检查模型列表接口。')
        name = getattr(item, 'display_name', None)
        models.setdefault(model_id, {
            'id': model_id,
            'display_name': name.strip() if isinstance(name, str) else '',
        })
    return sorted(models.values(), key=lambda model: model['id'].casefold())


def fetch_provider_models(provider):
    """沿用协议 SDK 的鉴权、Base URL 和分页；结果仅供当前页面选择。"""
    try:
        provider.clean()
    except ValidationError as exc:
        raise LLMModelDiscoveryError('连接配置有误：' + '；'.join(exc.messages)) from exc
    key_optional = provider.api_type in (LLMProvider.API_OPENAI, LLMProvider.API_OPENAI_RESPONSES) and provider.base_url
    if not provider.api_key and not key_optional:
        raise LLMModelDiscoveryError('获取模型列表前请填写 API Key。')
    options = {
        'api_key': provider.api_key or 'not-required',
        'timeout': provider.timeout,
        'max_retries': provider.max_retries,
    }
    try:
        if provider.api_type == LLMProvider.API_ANTHROPIC:
            from anthropic import Anthropic

            with Anthropic(base_url=provider.base_url or 'https://api.anthropic.com', **options) as client:
                return _normalize_models(_page_models(client.models.list(limit=1000)))
        if provider.api_type == LLMProvider.API_GEMINI:
            from google import genai
            from google.genai import types

            http_options = types.HttpOptions(
                base_url=provider.base_url or 'https://generativelanguage.googleapis.com',
                api_version='v1beta', timeout=provider.timeout * 1000,
                retry_options=types.HttpRetryOptions(attempts=provider.max_retries + 1),
            )
            with genai.Client(api_key=provider.api_key, vertexai=False, http_options=http_options) as client:
                return _normalize_models(client.models.list(config={'page_size': 1000}), gemini=True)

        from openai import OpenAI

        with OpenAI(base_url=provider.base_url or 'https://api.openai.com/v1', **options) as client:
            return _normalize_models(_page_models(client.models.list()))
    except LLMModelDiscoveryError:
        raise
    except Exception as exc:
        logger.warning('获取 LLM 模型列表失败，接口类型=%s，错误类型=%s', provider.api_type, type(exc).__name__)
        status = getattr(exc, 'status_code', None) or getattr(exc, 'code', None)
        if status in (401, 403):
            message = '供应商鉴权失败，请检查 API Key 和模型列表访问权限。'
        elif status in (404, 405, 501):
            message = '供应商未提供可用的模型列表接口，请检查 Base URL，或使用「添加模型」手动配置。'
        elif status == 429:
            message = '供应商请求过于频繁，请稍后重新获取模型列表。'
        elif 'timeout' in type(exc).__name__.lower():
            message = '获取模型列表超时，请检查供应商网络连接或调整超时设置。'
        else:
            message = '获取模型列表失败，请检查供应商地址、密钥和网络连接；也可以手动添加模型。'
        raise LLMModelDiscoveryError(message) from exc
