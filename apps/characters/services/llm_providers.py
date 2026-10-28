"""供应商协议适配与模型降级；每次请求从数据库加载候选模型。"""

import logging

from django.core.exceptions import ValidationError

from apps.characters.models import LLMProvider, LLMRoutingEntry

logger = logging.getLogger(__name__)


class LLMConfigurationError(ValueError):
    pass


class LLMResponseError(ValueError):
    pass


class LLMFallbackError(RuntimeError):
    def __init__(self, failures):
        self.failures = failures
        details = '；'.join(f"{item['provider']} / {item['model']}: {item['error']}" for item in failures)
        super().__init__('所有候选 LLM 模型均调用失败：' + details)


def get_available_models():
    """仅取独立调用链中的模型；一次查询获取最新配置及供应商。"""
    entries = LLMRoutingEntry.objects.select_related('model__provider').filter(
        config_id=1, config__is_active=True,
        model__is_active=True, model__provider__is_active=True,
    ).order_by('priority', 'pk')
    return [entry.model for entry in entries]


def create_chat_model(config, *, max_tokens=None, temperature=None, max_retries=None):
    provider = config.provider
    if not config.is_active or not provider.is_active:
        raise LLMConfigurationError('模型或供应商已停用，请在 Django 后台启用。')
    try:
        provider.clean()
        config.clean()
    except ValidationError as exc:
        raise LLMConfigurationError('LLM 配置无效：' + '；'.join(exc.messages)) from exc

    options = {
        'model': config.model,
        'api_key': provider.api_key or 'not-required',
        'max_tokens': min(max_tokens, config.max_tokens) if max_tokens is not None else config.max_tokens,
        'timeout': provider.timeout,
        'max_retries': provider.max_retries if max_retries is None else max_retries,
    }
    effective_temperature = config.temperature if config.temperature is not None else temperature
    if effective_temperature is not None:
        options['temperature'] = effective_temperature

    if provider.api_type == LLMProvider.API_ANTHROPIC:
        from langchain_anthropic import ChatAnthropic

        options['base_url'] = provider.base_url or 'https://api.anthropic.com'
        return ChatAnthropic(**options)
    if provider.api_type == LLMProvider.API_GEMINI:
        from langchain_google_genai import ChatGoogleGenerativeAI

        options['base_url'] = provider.base_url or 'https://generativelanguage.googleapis.com'
        # Google SDK 使用总尝试次数，1 表示不重试；0 会启用 SDK 默认重试。
        options['max_retries'] += 1
        options['vertexai'] = False
        return ChatGoogleGenerativeAI(**options)

    from langchain_openai import ChatOpenAI

    options['base_url'] = provider.base_url or 'https://api.openai.com/v1'
    options['use_responses_api'] = provider.api_type == LLMProvider.API_OPENAI_RESPONSES
    return ChatOpenAI(**options)


def call_llm_api(system_prompt, user_prompt, max_tokens=None, temperature=None, max_retries=None,
                 *, model_config=None, response_validator=None):
    """依优先级调用，重试耗尽或回复无效时降级；显式传入模型则只测试该模型。

    response_validator 接收正文，内容无效时应抛出异常。成功后在 response_metadata 中
    记录实际使用的供应商、模型和降级次数，避免历史记录仍指向首选模型。
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from .llm_utils import extract_text_from_response

    candidates = [model_config] if model_config is not None else get_available_models()
    if not candidates:
        raise LLMConfigurationError('调用链未启用或没有可用模型，请在后台「LLM 调用链配置」中选择模型，并启用相应供应商和模型。')
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    secrets = {candidate.provider.api_key.strip() for candidate in candidates if candidate.provider.api_key.strip()}
    failures = []
    for candidate in candidates:
        provider = candidate.provider
        try:
            client = create_chat_model(candidate, max_tokens=max_tokens, temperature=temperature, max_retries=max_retries)
            response = client.invoke(messages)
            text = extract_text_from_response(response)
            if not text:
                raise LLMResponseError('模型未返回正文。')
            if response_validator is not None:
                response_validator(text)
        except Exception as exc:
            detail = str(exc)
            for secret in sorted(secrets, key=len, reverse=True):
                detail = detail.replace(secret, '***')
            failures.append({
                'provider': provider.name, 'model': candidate.model,
                'error_type': type(exc).__name__, 'error': detail[:300],
            })
            logger.warning('LLM 调用失败，供应商=%s，模型=%s，错误类型=%s',
                           provider.name, candidate.model, type(exc).__name__)
            continue
        response.response_metadata.update({
            'llm_provider_id': provider.pk,
            'llm_provider': provider.name,
            'llm_model_id': candidate.pk,
            'llm_model': candidate.model,
            'llm_fallback_count': len(failures),
        })
        if failures:
            logger.info('LLM 已降级成功，供应商=%s，模型=%s，已跳过 %s 个失败模型',
                        provider.name, candidate.model, len(failures))
        return response
    raise LLMFallbackError(failures)
