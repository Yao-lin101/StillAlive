"""
向量存储后端抽象层。

长期重要事件记忆的语义检索可以使用不同的向量后端：

- ``milvus``   : 独立部署的 Milvus（适合较大规模 / 已有基础设施）。
- ``pgvector`` : 直接使用 Postgres 的 pgvector 扩展，零额外基础设施，适合个人 / 小数据量。
- ``none``     : 关闭语义检索，``search`` 返回空——上层 ``retrieve_important_events``
                  会自动退回到 importance + recency + 关键词的元数据兜底。

embedding 的生成（Ollama）留在 service 层（``important_event_service._ollama_embed``）；
后端只负责「存 / 删 / 查」向量本身，``search`` 接收已经算好的 query 向量。

通过 settings.VECTOR_BACKEND 选择后端，默认 ``auto``：
pgvector 库可用则用 pgvector，否则若配置了 Milvus 则用 Milvus，否则关闭。
"""

import logging

from django.conf import settings

from apps.characters.models import ImportantEvent

logger = logging.getLogger(__name__)

MILVUS_ALIAS = 'important_event_memory'


class VectorBackend:
    """向量后端接口。所有方法都必须容错：任何异常都不应让上层主流程崩溃。"""

    name = 'base'

    def is_available(self):
        return False

    def upsert(self, items):
        """items: List[Tuple[ImportantEvent, List[float]]]，返回成功写入条数。"""
        return 0

    def delete(self, event_id):
        """删除单个事件的向量，返回是否成功。"""
        return False

    def search(self, character, embedding, recall):
        """返回 {event_id: cosine_similarity(0-1，越大越相关)}。"""
        return {}

    def reset(self):
        """清空后端中的全部向量（用于 --clean 全量重建）。返回是否成功。"""
        return True


class NullBackend(VectorBackend):
    """关闭语义检索：search 永远返回空，等价于纯元数据兜底。"""

    name = 'none'

    def is_available(self):
        return True


class MilvusBackend(VectorBackend):
    """基于 Milvus 的向量后端（保留原有实现，供已有部署继续使用）。"""

    name = 'milvus'

    def is_available(self):
        return self._collection() is not None

    def _collection(self):
        try:
            from pymilvus import (
                Collection,
                CollectionSchema,
                DataType,
                FieldSchema,
                connections,
                utility,
            )
        except ImportError:
            logger.info("pymilvus is not installed; Milvus backend unavailable")
            return None

        collection_name = getattr(settings, 'MILVUS_IMPORTANT_EVENT_COLLECTION', 'important_events')
        uri = getattr(settings, 'MILVUS_URI', '')
        token = getattr(settings, 'MILVUS_TOKEN', '')
        db_name = getattr(settings, 'MILVUS_DB_NAME', '')

        try:
            if not connections.has_connection(MILVUS_ALIAS):
                if uri:
                    kwargs = {'alias': MILVUS_ALIAS, 'uri': uri}
                    if token:
                        kwargs['token'] = token
                    if db_name:
                        kwargs['db_name'] = db_name
                    connections.connect(**kwargs)
                else:
                    kwargs = {
                        'alias': MILVUS_ALIAS,
                        'host': getattr(settings, 'MILVUS_HOST', '127.0.0.1'),
                        'port': str(getattr(settings, 'MILVUS_PORT', '19530')),
                    }
                    if db_name:
                        kwargs['db_name'] = db_name
                    connections.connect(**kwargs)

            if not utility.has_collection(collection_name, using=MILVUS_ALIAS):
                dim = int(getattr(settings, 'OLLAMA_EMBED_DIM', 1024))
                fields = [
                    FieldSchema(name='event_id', dtype=DataType.VARCHAR, max_length=32, is_primary=True),
                    FieldSchema(name='character_uid', dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name='event_date', dtype=DataType.VARCHAR, max_length=10),
                    FieldSchema(name='importance_score', dtype=DataType.INT64),
                    FieldSchema(name='embedding', dtype=DataType.FLOAT_VECTOR, dim=dim),
                ]
                schema = CollectionSchema(fields=fields, description='StillAlive important event memory')
                collection = Collection(collection_name, schema=schema, using=MILVUS_ALIAS)
                collection.create_index(
                    field_name='embedding',
                    index_params={
                        'metric_type': 'COSINE',
                        'index_type': 'HNSW',
                        'params': {'M': 16, 'efConstruction': 200},
                    },
                )
            else:
                collection = Collection(collection_name, using=MILVUS_ALIAS)

            collection.load()
            return collection
        except Exception as exc:
            logger.warning("Milvus collection unavailable: %s", exc)
            return None

    def upsert(self, items):
        from django.db import transaction
        from django.utils import timezone

        if not items:
            return 0
        collection = self._collection()
        if collection is None:
            return 0

        ids, char_ids, dates, scores, embs, events = [], [], [], [], [], []
        for event, embedding in items:
            if not embedding:
                continue
            ids.append(str(event.id))
            char_ids.append(str(event.character_id))
            dates.append(event.date.isoformat())
            scores.append(int(event.importance_score))
            embs.append(embedding)
            events.append(event)

        if not ids:
            return 0

        try:
            id_str = ", ".join([f'"{i}"' for i in ids])
            collection.delete(f'event_id in [{id_str}]')
            collection.insert([ids, char_ids, dates, scores, embs])
            collection.flush()

            with transaction.atomic():
                now = timezone.now()
                for event in events:
                    event.milvus_synced = True
                    event.milvus_synced_at = now
                    event.save(update_fields=['milvus_synced', 'milvus_synced_at'])

            logger.info("Successfully synced %d events to Milvus", len(events))
            return len(events)
        except Exception as exc:
            logger.warning("Batch sync to Milvus failed: %s", exc)
            return 0

    def delete(self, event_id):
        collection = self._collection()
        if collection is None:
            return False
        try:
            collection.delete(f'event_id == "{event_id}"')
            collection.flush()
            return True
        except Exception as exc:
            logger.warning("Failed to delete important event %s from Milvus: %s", event_id, exc)
            return False

    def search(self, character, embedding, recall):
        if not embedding:
            return {}
        collection = self._collection()
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

    def reset(self):
        collection_name = getattr(settings, 'MILVUS_IMPORTANT_EVENT_COLLECTION', 'important_events')
        try:
            from pymilvus import utility
        except ImportError:
            return False
        try:
            if utility.has_collection(collection_name, using=MILVUS_ALIAS):
                utility.drop_collection(collection_name, using=MILVUS_ALIAS)
            # 重新建立空集合 + 索引
            return self._collection() is not None
        except Exception as exc:
            logger.warning("Failed to reset Milvus collection: %s", exc)
            return False


class PgvectorBackend(VectorBackend):
    """基于 Postgres pgvector 扩展的向量后端。embedding 与事件存在同一行。"""

    name = 'pgvector'

    def is_available(self):
        try:
            import pgvector.django  # noqa: F401
        except ImportError:
            logger.info("pgvector is not installed; pgvector backend unavailable")
            return False
        # embedding 列由迁移创建；若迁移未应用则字段不存在。
        return any(f.name == 'embedding' for f in ImportantEvent._meta.get_fields())

    def upsert(self, items):
        from django.db import transaction
        from django.utils import timezone

        if not items:
            return 0

        synced = 0
        try:
            with transaction.atomic():
                now = timezone.now()
                for event, embedding in items:
                    if not embedding:
                        continue
                    event.embedding = embedding
                    event.milvus_synced = True
                    event.milvus_synced_at = now
                    event.save(update_fields=['embedding', 'milvus_synced', 'milvus_synced_at'])
                    synced += 1
            logger.info("Successfully synced %d events to pgvector", synced)
            return synced
        except Exception as exc:
            logger.warning("Batch sync to pgvector failed: %s", exc)
            return 0

    def delete(self, event_id):
        # embedding 随事件行级联删除；若事件仍在则清空向量列。
        try:
            ImportantEvent.objects.filter(id=event_id).update(
                embedding=None, milvus_synced=False, milvus_synced_at=None
            )
            return True
        except Exception as exc:
            logger.warning("Failed to clear pgvector embedding for event %s: %s", event_id, exc)
            return False

    def search(self, character, embedding, recall):
        if not embedding:
            return {}
        try:
            from pgvector.django import CosineDistance
        except ImportError:
            return {}
        try:
            rows = (
                ImportantEvent.objects.filter(
                    character=character, is_active=True, embedding__isnull=False
                )
                .annotate(distance=CosineDistance('embedding', embedding))
                .order_by('distance')
                .values_list('id', 'distance')[:recall]
            )
            # pgvector cosine distance ∈ [0,2]，相似度 = 1 - distance（与 Milvus COSINE 对齐到 0-1 区间）。
            return {int(i): max(0.0, 1.0 - float(d)) for i, d in rows}
        except Exception as exc:
            logger.warning("pgvector important event search failed: %s", exc)
            return {}

    def reset(self):
        try:
            ImportantEvent.objects.exclude(embedding__isnull=True).update(
                embedding=None, milvus_synced=False, milvus_synced_at=None
            )
            return True
        except Exception as exc:
            logger.warning("Failed to reset pgvector embeddings: %s", exc)
            return False


_BACKEND_CACHE = None


def _resolve_backend():
    choice = str(getattr(settings, 'VECTOR_BACKEND', 'auto')).strip().lower()

    if choice == 'none':
        return NullBackend()
    if choice == 'milvus':
        return MilvusBackend()
    if choice == 'pgvector':
        return PgvectorBackend()

    # auto: 优先 pgvector（零基础设施），否则 Milvus（若配置/可连），否则关闭。
    pg = PgvectorBackend()
    if pg.is_available():
        logger.info("Vector backend auto-selected: pgvector")
        return pg
    milvus = MilvusBackend()
    if milvus.is_available():
        logger.info("Vector backend auto-selected: milvus")
        return milvus
    logger.info("Vector backend auto-selected: none (semantic memory disabled)")
    return NullBackend()


def get_vector_backend(force_reload=False):
    """返回当前进程使用的向量后端单例。"""
    global _BACKEND_CACHE
    if force_reload or _BACKEND_CACHE is None:
        _BACKEND_CACHE = _resolve_backend()
    return _BACKEND_CACHE
