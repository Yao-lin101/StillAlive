"""
嵌入（embedding）提供商抽象层。

支持两类后端，运行时由 `EmbeddingConfig`（admin 可配置的单例）决定：

- ``ollama`` : 调用 Ollama ``POST {base_url}/api/embed``。
- ``openai`` : 调用任意 OpenAI 兼容端点 ``POST {base_url}/embeddings``（Bearer 鉴权），
               base_url 可指向官方 / Azure / vLLM / LocalAI / 硅基流动等。

对外统一入口 `generate_embedding(text)`，契约与原 `_ollama_embed` 完全一致：
- 传入 str  → 返回单个向量 ``[float]``
- 传入 list → 返回向量列表 ``[[float], ...]``
- 任意失败 → 返回 ``None``（容错，不抛异常）

配置每次从 DB 读取（单例 PK 查询，开销可忽略），因此 admin 改完即时生效，
且多进程（gunicorn / celery）下不会有陈旧的模块级缓存。
"""

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)


def get_embedding_config():
    """返回 admin 可配置的嵌入单例（不存在则用 settings 播种）。"""
    from apps.characters.models import EmbeddingConfig
    return EmbeddingConfig.load()


class EmbeddingProvider:
    """嵌入提供商基类。"""

    def __init__(self, config):
        self.config = config

    def _truncate(self, text):
        """按 config.max_chars 截断；支持 str 或 list（递归）。"""
        if isinstance(text, list):
            return [self._truncate(t) for t in text]
        text = str(text or '').strip()
        max_chars = int(self.config.max_chars or 0)
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return text[:max_chars]

    def embed(self, text):
        raise NotImplementedError

    @staticmethod
    def _post_json(url, payload, headers, timeout):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(url, data=data, headers=headers, method='POST')
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))


class OllamaEmbeddingProvider(EmbeddingProvider):
    name = 'ollama'

    def embed(self, text):
        if not text:
            return None
        base_url = (self.config.base_url or 'http://127.0.0.1:11434').rstrip('/')
        payload = {'model': self.config.model, 'input': self._truncate(text)}
        try:
            data = self._post_json(
                f'{base_url}/api/embed',
                payload,
                {'Content-Type': 'application/json'},
                int(self.config.timeout or 60),
            )
        except Exception as exc:
            logger.warning("Ollama embedding request failed: %s", exc)
            return None

        embeddings = data.get('embeddings')
        if embeddings and isinstance(embeddings, list):
            return [[float(x) for x in emb] for emb in embeddings]
        embedding = data.get('embedding')
        if isinstance(embedding, list):
            return [float(x) for x in embedding]
        return None


class OpenAIEmbeddingProvider(EmbeddingProvider):
    name = 'openai'

    def embed(self, text):
        if not text:
            return None
        base_url = (self.config.base_url or 'https://api.openai.com/v1').rstrip('/')
        is_batch = isinstance(text, list)
        payload = {'model': self.config.model, 'input': self._truncate(text)}
        # dimensions 仅 text-embedding-3* 系列支持；其他模型/兼容端可能拒绝该参数。
        if self.config.dimensions and str(self.config.model).startswith('text-embedding-3'):
            payload['dimensions'] = int(self.config.dimensions)

        headers = {'Content-Type': 'application/json'}
        if self.config.api_key:
            headers['Authorization'] = f'Bearer {self.config.api_key}'

        try:
            data = self._post_json(
                f'{base_url}/embeddings',
                payload,
                headers,
                int(self.config.timeout or 60),
            )
        except Exception as exc:
            logger.warning("OpenAI-compatible embedding request failed: %s", exc)
            return None

        items = data.get('data')
        if not items or not isinstance(items, list):
            return None
        # 按 index 排序，保证与输入顺序一致
        items = sorted(items, key=lambda d: d.get('index', 0))
        vectors = [[float(x) for x in d.get('embedding', [])] for d in items]
        if not vectors:
            return None
        return vectors if is_batch else vectors[0]


def get_embedding_provider():
    config = get_embedding_config()
    if config.provider == config.PROVIDER_OPENAI:
        return OpenAIEmbeddingProvider(config)
    return OllamaEmbeddingProvider(config)


def generate_embedding(text):
    """对外统一入口：生成嵌入向量（str→单向量 / list→向量列表 / 失败→None）。"""
    if not text:
        return None
    return get_embedding_provider().embed(text)
