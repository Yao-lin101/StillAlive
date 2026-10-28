import logging
import threading

from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponseNotAllowed, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from .models import EmbeddingConfig, ImportantEvent, LLMModel, LLMProvider, LLMRoutingConfig, LLMRoutingEntry

logger = logging.getLogger(__name__)


class LLMProviderAdminForm(forms.ModelForm):
    api_key = forms.CharField(
        label='API Key', required=False, max_length=2048,
        widget=forms.PasswordInput(render_value=False),
        help_text='留空保留已保存的密钥；填写新值则替换。密钥以明文存储在数据库中。',
    )
    clear_api_key = forms.BooleanField(label='清除已保存的密钥', required=False)

    class Meta:
        model = LLMProvider
        fields = '__all__'

    def clean(self):
        data = super().clean()
        if data.get('clear_api_key'):
            if data.get('api_key'):
                self.add_error('api_key', '不能同时填写新密钥并勾选清除。')
            data['api_key'] = ''
        elif not data.get('api_key'):
            data['api_key'] = self.instance.api_key
        return data


class LLMProviderDiscoveryForm(LLMProviderAdminForm):
    class Meta:
        model = LLMProvider
        fields = ('api_type', 'base_url', 'api_key', 'timeout', 'max_retries')


class LLMModelInlineFormSet(BaseInlineFormSet):
    def clean(self):
        for form in self.forms:
            if (getattr(form, 'cleaned_data', {}).get('DELETE') and form.instance.pk
                    and form.instance.routing_entries.exists()):
                raise ValidationError('模型仍在调用链中使用，请先将它移出调用链，再删除模型。')
        super().clean()


class LLMModelInline(admin.StackedInline):
    model = LLMModel
    formset = LLMModelInlineFormSet
    template = 'admin/characters/llmprovider/model_inline.html'
    fields = ('model', 'display_name', 'is_active', 'max_tokens', 'temperature')
    extra = 0
    show_change_link = False
    ordering = ('pk',)
    verbose_name = '模型'
    verbose_name_plural = '已配置的模型'

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(route_count=Count('routing_entries'))

    def _provider_admin(self):
        return self.admin_site._registry[LLMProvider]

    def has_view_permission(self, request, obj=None):
        return self._provider_admin().has_view_or_change_permission(request, obj)

    def has_add_permission(self, request, obj=None):
        if obj is None:
            return self._provider_admin().has_add_permission(request)
        return self._provider_admin().has_change_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._provider_admin().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._provider_admin().has_change_permission(request, obj)


def _llm_model_connection_result(config):
    from .services.llm_providers import call_llm_api

    try:
        call_llm_api(
            system_prompt='This is a connection test. Reply with OK only.',
            user_prompt='Please reply OK.',
            max_tokens=1024,
            temperature=0,
            max_retries=0,
            model_config=config,
        )
    except Exception as exc:
        detail = str(exc)
        if config.provider.api_key:
            detail = detail.replace(config.provider.api_key, '***')
        return False, f'{config} 连接测试失败：{detail}'
    return True, f'连接成功：{config}'


def _test_llm_model_connection(model_admin, request, config):
    ok, message = _llm_model_connection_result(config)
    model_admin.message_user(request, message, level='success' if ok else 'error')


@admin.register(LLMProvider)
class LLMProviderAdmin(admin.ModelAdmin):
    change_form_template = 'admin/characters/llmprovider/change_form.html'
    form = LLMProviderAdminForm
    list_display = ('name', 'api_type', 'base_url', 'is_active', 'updated_at')
    list_filter = ('api_type', 'is_active')
    search_fields = ('name', 'base_url')
    readonly_fields = ('updated_at', 'api_key_status')
    inlines = [LLMModelInline]
    actions = ['test_connection']


    class Media:
        css = {'all': ('characters/admin/llm_provider.css',)}
        js = ('characters/admin/llm_provider.js',)

    def get_urls(self):
        return [
            path('discover-models/', self.admin_site.admin_view(self.discover_models_view),
                 name='characters_llmprovider_discover_models_add'),
            path('<int:provider_id>/discover-models/', self.admin_site.admin_view(self.discover_models_view),
                 name='characters_llmprovider_discover_models'),
            path('<int:provider_id>/models/<int:model_id>/test/',
                 self.admin_site.admin_view(self.test_model_connection_view),
                 name='characters_llmprovider_model_test'),
        ] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        if request.method == 'GET' and not request.GET and self.has_view_or_change_permission(request):
            provider = self.get_queryset(request).first()
            if provider is not None:
                return HttpResponseRedirect(reverse('admin:characters_llmprovider_change', args=[provider.pk]))
            if self.has_add_permission(request):
                return HttpResponseRedirect(reverse('admin:characters_llmprovider_add'))
        return super().changelist_view(request, extra_context)

    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        context['provider_sources'] = (
            self.get_queryset(request).annotate(model_count=Count('models'))
            if self.has_view_or_change_permission(request) else []
        )
        context['can_add_provider'] = self.has_add_permission(request)
        context['current_provider'] = obj
        context['model_discovery_url'] = reverse(
            'admin:characters_llmprovider_discover_models' if obj else 'admin:characters_llmprovider_discover_models_add',
            args=[obj.pk] if obj else None,
        )
        return super().render_change_form(request, context, add, change, form_url, obj)

    def discover_models_view(self, request, provider_id=None):
        from .services.llm_model_catalog import LLMModelDiscoveryError, fetch_provider_models

        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])
        if provider_id is None:
            if not self.has_add_permission(request):
                raise PermissionDenied
            provider = LLMProvider(name='未保存的供应商')
        else:
            provider = get_object_or_404(self.get_queryset(request), pk=provider_id)
            if not self.has_change_permission(request, provider):
                raise PermissionDenied
        saved_key = provider.api_key
        form = LLMProviderDiscoveryForm(data=request.POST, instance=provider)
        if not form.is_valid():
            return JsonResponse({'ok': False, 'message': '连接配置有误，请检查后重试。',
                                 'errors': form.errors.get_json_data()}, status=400)
        try:
            models = fetch_provider_models(form.instance)
        except Exception as exc:
            message = str(exc) if isinstance(exc, LLMModelDiscoveryError) else '获取模型列表失败，请检查供应商连接后重试。'
            for key in sorted({key for key in (saved_key, form.instance.api_key) if key}, key=len, reverse=True):
                message = message.replace(key, '***')
            return JsonResponse({'ok': False, 'message': message}, status=502)
        return JsonResponse({'ok': True, 'models': models})

    def test_model_connection_view(self, request, provider_id, model_id):
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])
        provider = get_object_or_404(self.get_queryset(request), pk=provider_id)
        if not self.has_change_permission(request, provider):
            raise PermissionDenied
        config = get_object_or_404(LLMModel.objects.select_related('provider'), pk=model_id, provider_id=provider.pk)
        ok, message = _llm_model_connection_result(config)
        return JsonResponse({'ok': ok, 'message': message}, status=200 if ok else 400)

    @admin.display(description='API Key')
    def api_key_status(self, obj):
        return '已配置' if obj and obj.api_key else '未配置'

    def get_fieldsets(self, request, obj=None):
        fieldsets = [
            ('供应商配置', {'fields': ('name', 'api_type', 'base_url', 'api_key', 'clear_api_key', 'is_active',
                                   'timeout', 'max_retries', 'updated_at'),
                            'classes': ('collapse',) if obj is not None else ()}),
        ]
        if not self.has_change_permission(request, obj) and not (obj is None and self.has_add_permission(request)):
            return [
                (title, {**options, 'fields': tuple(
                    'api_key_status' if field == 'api_key' else field
                    for field in options['fields'] if field != 'clear_api_key'
                )})
                for title, options in fieldsets
            ]
        return fieldsets

    @admin.action(description='测试供应商连接（使用其首个启用模型）', permissions=['change'])
    def test_connection(self, request, queryset):
        for provider in queryset:
            config = provider.models.filter(is_active=True).select_related('provider').order_by('pk').first()
            if config is None:
                self.message_user(request, f'{provider} 尚无启用模型，请先添加模型。', level='error')
                continue
            _test_llm_model_connection(self, request, config)


class OrderedLLMModelSelect(forms.SelectMultiple):
    template_name = 'admin/characters/widgets/ordered_llm_models.html'

    class Media:
        css = {'all': ('characters/admin/llm_routing.css',)}
        js = ('characters/admin/llm_routing.js',)

    def id_for_label(self, id_):
        return f'{id_}_picker' if id_ else id_

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        options = [option for _, group, _ in context['widget']['optgroups'] for option in group]
        by_id = {str(option['value']): option for option in options}
        selected_ids = [str(model_id) for model_id in value or []]
        selected = [by_id[model_id] for model_id in selected_ids if model_id in by_id]
        context['widget']['ordered_options'] = selected + [
            option for option in options if str(option['value']) not in selected_ids
        ]
        return context


class OrderedLLMModelsField(forms.ModelMultipleChoiceField):
    widget = OrderedLLMModelSelect

    def label_from_instance(self, obj):
        suffix = '（模型或供应商已停用）' if not obj.is_active or not obj.provider.is_active else ''
        name = f'{obj.provider}: {obj.model}'
        if obj.display_name:
            name += f'（{obj.display_name}）'
        return f'{name}{suffix}'

    def clean(self, value):
        # Django 的默认多选字段会返回无序 QuerySet；这里显式保留提交顺序。
        models = super().clean(value)
        ids = [str(self.queryset.model._meta.pk.to_python(model_id)) for model_id in value or []]
        if len(ids) != len(set(ids)):
            raise ValidationError('同一个模型只能加入调用链一次。')
        by_id = {str(model.pk): model for model in models}
        return [by_id[model_id] for model_id in ids]

    def has_changed(self, initial, data):
        if self.disabled:
            return False
        return [str(value) for value in initial or []] != [str(value) for value in data or []]


class LLMRoutingConfigAdminForm(forms.ModelForm):
    model_order = OrderedLLMModelsField(
        label='调用模型', required=False, queryset=LLMModel.objects.none(),
        help_text='选择模型后自动追加到末尾。拖拽或使用上下按钮调整顺序；第一项为首选，其余依次降级。',
    )

    class Meta:
        model = LLMRoutingConfig
        fields = ('is_active', 'model_order')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['model_order'].queryset = LLMModel.objects.select_related('provider').order_by(
            'provider__name', 'model', 'pk',
        )
        if self.instance.pk:
            self.initial['model_order'] = list(self.instance.entries.order_by('priority', 'pk').values_list(
                'model_id', flat=True,
            ))

    def _save_m2m(self):
        super()._save_m2m()
        with transaction.atomic():
            # 整条调用链作为一次更新写入，其他请求不会读到保存到一半的顺序。
            LLMRoutingConfig.objects.select_for_update().get(pk=self.instance.pk)
            self.instance.entries.all().delete()
            LLMRoutingEntry.objects.bulk_create([
                LLMRoutingEntry(config=self.instance, model=model, priority=index)
                for index, model in enumerate(self.cleaned_data['model_order'], 1)
            ])


@admin.register(LLMRoutingConfig)
class LLMRoutingConfigAdmin(admin.ModelAdmin):
    form = LLMRoutingConfigAdminForm
    list_display = ('__str__', 'is_active', 'updated_at')
    readonly_fields = ('updated_at',)
    fields = ('is_active', 'model_order', 'updated_at')

    @admin.display(description='调用模型')
    def model_order(self, obj):
        if not obj or not obj.pk:
            return '尚未加入模型'
        entries = list(obj.entries.select_related('model__provider').order_by('priority', 'pk'))
        if not entries:
            return '尚未加入模型'
        return format_html('<ol>{}</ol>', format_html_join(
            '', '<li>#{0} {1} — {2}: {3}</li>',
            ((index, '首选' if index == 1 else '备用', entry.model.provider.name, entry.model.model)
             for index, entry in enumerate(entries, 1)),
        ))

    def has_add_permission(self, request):
        return not LLMRoutingConfig.objects.exists() and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


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
