import logging
import threading

from django import forms
from django.contrib import admin

from .models import EmbeddingConfig, ImportantEvent

logger = logging.getLogger(__name__)


@admin.register(ImportantEvent)
class ImportantEventAdmin(admin.ModelAdmin):
    list_display = (
        'character',
        'date',
        'title',
        'event_type',
        'importance_score',
        'confidence',
        'is_active',
        'milvus_synced',
    )
    list_filter = ('is_active', 'milvus_synced', 'event_type', 'date')
    search_fields = ('title', 'summary', 'event_key')
    readonly_fields = ('created_at', 'updated_at', 'milvus_synced_at')


def _run_rebuild_in_background():
    """后台线程执行全量重建（参照 apps.ready() 的 infra-check 线程模式）。"""
    from django.core.management import call_command
    try:
        call_command('rebuild_milvus_memory', clean=True, force=True)
        logger.info("Embedding rebuild finished")
    except Exception as exc:
        logger.error("Embedding rebuild failed: %s", exc)


@admin.register(EmbeddingConfig)
class EmbeddingConfigAdmin(admin.ModelAdmin):
    list_display = ('provider', 'model', 'base_url', 'dimensions', 'is_active', 'updated_at')
    readonly_fields = ('updated_at',)
    actions = ['rebuild_embeddings', 'test_connection']

    def has_add_permission(self, request):
        # 单例：已存在时不允许再新增
        return not EmbeddingConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == 'api_key':
            kwargs['widget'] = forms.PasswordInput(render_value=True)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    @admin.action(description='重建向量（改了模型/维度后必须执行）')
    def rebuild_embeddings(self, request, queryset):
        thread = threading.Thread(
            target=_run_rebuild_in_background,
            name='embedding-rebuild',
            daemon=True,
        )
        thread.start()
        self.message_user(
            request,
            '已在后台开始重建全部事件的向量。数据量大时需要一些时间，完成后语义检索即恢复正常。',
        )

    @admin.action(description='测试连接（用当前配置生成一次嵌入）')
    def test_connection(self, request, queryset):
        from .services.embedding_providers import generate_embedding, get_embedding_config
        cfg = get_embedding_config()
        try:
            vec = generate_embedding('连接测试 connection test')
        except Exception as exc:
            self.message_user(request, f'测试失败：{exc}', level='error')
            return
        if vec:
            self.message_user(
                request,
                f'测试成功：provider={cfg.provider} model={cfg.model} 返回维度={len(vec)}',
                level='success',
            )
        else:
            self.message_user(
                request,
                f'测试失败：未返回向量，请检查 base_url / api_key / model（provider={cfg.provider}, base_url={cfg.base_url}）',
                level='error',
            )
