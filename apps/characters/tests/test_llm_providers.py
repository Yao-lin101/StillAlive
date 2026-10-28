import importlib
import json
import os
from contextlib import ExitStack, redirect_stdout
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from django import forms
from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from langchain_core.messages import AIMessage

from apps.characters.admin import LLMProviderAdmin, LLMProviderAdminForm, LLMRoutingConfigAdminForm
from apps.characters.models import LLMModel, LLMProvider, LLMRoutingConfig, LLMRoutingEntry
from apps.characters.services.llm_providers import (
    LLMConfigurationError, LLMFallbackError, call_llm_api, create_chat_model, get_available_models,
)
from apps.characters.services.llm_utils import (
    extract_text_from_response, safe_json_loads, validate_json_array, validate_json_object,
)


def reset_llm_catalog():
    LLMRoutingEntry.objects.all().delete()
    LLMProvider.objects.all().delete()
    LLMRoutingConfig.objects.update_or_create(pk=1, defaults={'is_active': True})


def configure_route(*models):
    config, _ = LLMRoutingConfig.objects.get_or_create(pk=1)
    for index, model in enumerate(models, 1):
        LLMRoutingEntry.objects.create(config=config, model=model, priority=index * 10)


class LLMProviderModelTests(TestCase):
    def setUp(self):
        reset_llm_catalog()
        self.provider = LLMProvider.objects.create(name='Supplier A', api_type='openai', api_key='db-key')

    def test_multiple_models_share_provider_credentials(self):
        first = LLMModel.objects.create(provider=self.provider, model='primary')
        second = LLMModel.objects.create(provider=self.provider, model='backup')
        self.assertEqual(list(self.provider.models.order_by('pk')), [first, second])
        configure_route(first, second)
        with self.assertNumQueries(1):
            candidates = get_available_models()
            self.assertEqual([item.provider.api_key for item in candidates], ['db-key', 'db-key'])

    def test_model_id_is_unique_within_supplier_but_allowed_across_suppliers(self):
        LLMModel.objects.create(provider=self.provider, model='same-model')
        other = LLMProvider.objects.create(name='Supplier B', api_type='openai', api_key='other-key')
        LLMModel.objects.create(provider=other, model='same-model')
        with self.assertRaises(IntegrityError), transaction.atomic():
            LLMModel.objects.create(provider=self.provider, model='same-model')
        self.assertEqual(LLMModel.objects.count(), 2)

    @override_settings(ANTHROPIC_API_KEY='stale-env-key', ANTHROPIC_MODEL='stale-model')
    def test_calls_reload_models_and_credentials_without_restart(self):
        first = LLMModel.objects.create(provider=self.provider, model='first')
        second = LLMModel.objects.create(provider=self.provider, model='second')
        configure_route(first, second)
        client = Mock()
        client.invoke.side_effect = [AIMessage(content='OK'), AIMessage(content='OK')]
        with patch('apps.characters.services.llm_providers.create_chat_model', return_value=client) as factory:
            call_llm_api('system', 'user')
            LLMRoutingEntry.objects.filter(model_id=second.pk).update(priority=0)
            LLMProvider.objects.filter(pk=self.provider.pk).update(api_key='updated-db-key')
            call_llm_api('system', 'user')
        a, b = [call.args[0] for call in factory.call_args_list]
        self.assertEqual((a.pk, a.provider.api_key), (first.pk, 'db-key'))
        self.assertEqual((b.pk, b.provider.api_key), (second.pk, 'updated-db-key'))

    def test_equal_priorities_have_stable_id_order(self):
        a = LLMModel.objects.create(provider=self.provider, model='a')
        b = LLMModel.objects.create(provider=self.provider, model='b')
        configure_route(a, b)
        LLMRoutingEntry.objects.all().update(priority=10)
        self.assertEqual([item.pk for item in get_available_models()], [a.pk, b.pk])


class LLMFallbackTests(TestCase):
    def setUp(self):
        reset_llm_catalog()
        self.a = LLMProvider.objects.create(name='A', api_type='openai', api_key='secret-a', max_retries=0)
        self.b = LLMProvider.objects.create(name='B', api_type='gemini', api_key='secret-b', max_retries=0)
        self.primary = LLMModel.objects.create(provider=self.a, model='primary')
        self.same_supplier = LLMModel.objects.create(provider=self.a, model='same-supplier-backup')
        self.other_supplier = LLMModel.objects.create(provider=self.b, model='other-supplier-backup')
        configure_route(self.primary, self.same_supplier, self.other_supplier)

    def clients(self, *outcomes):
        clients = []
        for outcome in outcomes:
            client = Mock()
            if isinstance(outcome, Exception):
                client.invoke.side_effect = outcome
            else:
                client.invoke.return_value = outcome
            clients.append(client)
        return patch('apps.characters.services.llm_providers.create_chat_model', side_effect=clients)

    def test_successful_primary_does_not_call_backups(self):
        with self.clients(AIMessage(content='OK', response_metadata={'usage': 'preserved'})) as factory:
            result = call_llm_api('system', 'user')
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(result.response_metadata['llm_model_id'], self.primary.pk)
        self.assertEqual(result.response_metadata['llm_fallback_count'], 0)
        self.assertEqual(result.response_metadata['usage'], 'preserved')

    def test_failure_falls_back_to_next_model_of_same_supplier(self):
        with self.clients(TimeoutError('timeout'), AIMessage(content='OK')) as factory:
            result = call_llm_api('system', 'user')
        self.assertEqual([call.args[0].pk for call in factory.call_args_list],
                         [self.primary.pk, self.same_supplier.pk])
        self.assertEqual(result.response_metadata['llm_fallback_count'], 1)
        self.assertEqual(result.response_metadata['llm_provider'], 'A')

    def test_failures_fall_back_across_suppliers(self):
        with self.clients(TimeoutError('timeout'), ValueError('model not available'), AIMessage(content='OK')) as factory:
            result = call_llm_api('system', 'user')
        self.assertEqual(factory.call_count, 3)
        self.assertEqual(result.response_metadata['llm_provider'], 'B')
        self.assertEqual(result.response_metadata['llm_model'], 'other-supplier-backup')
        self.assertEqual(result.response_metadata['llm_fallback_count'], 2)

    def test_invalid_configuration_does_not_block_next_supplier(self):
        self.a.api_key = ''
        self.a.save()
        successful_client = Mock()
        successful_client.invoke.return_value = AIMessage(content='OK')
        real_factory = create_chat_model

        def factory(candidate, **kwargs):
            if candidate.provider_id == self.a.pk:
                return real_factory(candidate, **kwargs)
            return successful_client

        with patch('apps.characters.services.llm_providers.create_chat_model', side_effect=factory):
            result = call_llm_api('system', 'user')
        self.assertEqual(result.response_metadata['llm_provider'], 'B')

    def test_empty_or_thinking_only_response_triggers_fallback(self):
        for content in ([], [{'type': 'thinking', 'thinking': 'hidden'}]):
            with self.subTest(content=content):
                with self.clients(AIMessage(content=content), AIMessage(content='OK')):
                    result = call_llm_api('system', 'user')
                self.assertEqual(result.response_metadata['llm_model_id'], self.same_supplier.pk)

    def test_invalid_json_triggers_fallback_with_task_validator(self):
        for text in ('not json', '[]'):
            with self.subTest(text=text):
                with self.clients(AIMessage(content=text), AIMessage(content='{"ok":true}')):
                    result = call_llm_api('system', 'user', response_validator=validate_json_object)
                self.assertEqual(result.response_metadata['llm_model_id'], self.same_supplier.pk)
                self.assertEqual(safe_json_loads(extract_text_from_response(result)), {'ok': True})

    def test_valid_empty_json_array_is_accepted(self):
        with self.clients(AIMessage(content='[]')) as factory:
            result = call_llm_api('system', 'user', response_validator=validate_json_array)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(extract_text_from_response(result), '[]')

    def test_disabled_models_and_suppliers_are_skipped(self):
        self.primary.is_active = False
        self.primary.save()
        self.b.is_active = False
        self.b.save()
        with self.clients(AIMessage(content='OK')) as factory:
            call_llm_api('system', 'user')
        self.assertEqual([call.args[0].pk for call in factory.call_args_list], [self.same_supplier.pk])

    def test_all_failed_errors_and_logs_redact_every_candidate_key(self):
        errors = [ValueError('failed secret-a secret-b') for _ in range(3)]
        with self.clients(*errors), self.assertLogs('apps.characters.services.llm_providers', level='WARNING') as logs:
            with self.assertRaises(LLMFallbackError) as caught:
                call_llm_api('system', 'user')
        self.assertEqual(len(caught.exception.failures), 3)
        text = str(caught.exception) + str(caught.exception.failures) + '\n'.join(logs.output)
        self.assertNotIn('secret-a', text)
        self.assertNotIn('secret-b', text)
        self.assertIn('other-supplier-backup', str(caught.exception))

    @override_settings(ANTHROPIC_API_KEY='old-env-key')
    def test_no_active_models_does_not_recreate_environment_configuration(self):
        reset_llm_catalog()
        with patch('apps.characters.services.llm_providers.create_chat_model') as factory:
            with self.assertRaises(LLMConfigurationError):
                call_llm_api('system', 'user')
        factory.assert_not_called()
        self.assertFalse(LLMProvider.objects.exists())

    def test_explicit_connection_test_never_hides_failure_with_another_model(self):
        with self.clients(TimeoutError('unavailable')) as factory:
            with self.assertRaises(LLMFallbackError) as caught:
                call_llm_api('system', 'user', model_config=self.primary)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(len(caught.exception.failures), 1)


class LLMRoutingTests(TestCase):
    def setUp(self):
        reset_llm_catalog()
        self.config = LLMRoutingConfig.objects.get(pk=1)
        a = LLMProvider.objects.create(name='A', api_type='openai', api_key='a-key')
        b = LLMProvider.objects.create(name='B', api_type='anthropic', api_key='b-key')
        self.primary = LLMModel.objects.create(provider=a, model='primary')
        self.unselected = LLMModel.objects.create(provider=a, model='configured-but-unselected')
        self.backup = LLMModel.objects.create(provider=b, model='backup')
        configure_route(self.primary, self.backup)

    def test_only_selected_models_participate_in_the_calling_chain(self):
        self.assertEqual([model.pk for model in get_available_models()], [self.primary.pk, self.backup.pk])
        self.assertTrue(self.unselected.is_active)
        self.assertEqual(LLMModel.objects.count(), 3)

    def test_new_enabled_model_is_not_automatically_added(self):
        added = LLMModel.objects.create(provider=self.primary.provider, model='new-model', is_active=True)
        self.assertNotIn(added.pk, [model.pk for model in get_available_models()])
        self.assertFalse(added.routing_entries.exists())

    def test_unselected_models_are_not_used_when_the_selected_chain_fails(self):
        client = Mock()
        client.invoke.side_effect = TimeoutError('unavailable')
        with patch('apps.characters.services.llm_providers.create_chat_model', return_value=client) as factory:
            with self.assertRaises(LLMFallbackError) as caught:
                call_llm_api('system', 'user')
        self.assertEqual([call.args[0].pk for call in factory.call_args_list], [self.primary.pk, self.backup.pk])
        self.assertEqual(len(caught.exception.failures), 2)

    def test_disabled_or_empty_chain_does_not_fall_back_to_the_model_catalog(self):
        self.config.is_active = False
        self.config.save()
        with patch('apps.characters.services.llm_providers.create_chat_model') as factory:
            with self.assertRaises(LLMConfigurationError):
                call_llm_api('system', 'user')
            self.config.is_active = True
            self.config.save()
            self.config.entries.all().delete()
            with self.assertRaises(LLMConfigurationError):
                call_llm_api('system', 'user')
        factory.assert_not_called()
        self.assertEqual(LLMModel.objects.filter(is_active=True).count(), 3)

    def test_removing_entry_preserves_model_configuration(self):
        self.config.entries.filter(model=self.primary).delete()
        self.primary.refresh_from_db()
        self.assertTrue(self.primary.is_active)
        self.assertEqual(self.primary.provider.api_key, 'a-key')
        self.assertEqual([model.pk for model in get_available_models()], [self.backup.pk])

    def test_same_model_cannot_be_selected_twice(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            LLMRoutingEntry.objects.create(config=self.config, model=self.primary, priority=30)
        self.assertEqual(self.config.entries.count(), 2)

    def test_selected_model_must_be_removed_from_chain_before_deletion(self):
        with self.assertRaises(ProtectedError):
            self.primary.delete()
        self.assertTrue(self.config.entries.filter(model=self.primary).exists())

    def test_connection_test_can_invoke_a_model_outside_the_chain(self):
        client = Mock()
        client.invoke.return_value = AIMessage(content='OK')
        with patch('apps.characters.services.llm_providers.create_chat_model', return_value=client):
            result = call_llm_api('system', 'user', model_config=self.unselected)
        self.assertEqual(result.response_metadata['llm_model_id'], self.unselected.pk)
        self.assertFalse(self.unselected.routing_entries.exists())


class LLMValidationTests(SimpleTestCase):
    def test_custom_base_url_accepts_internal_hosts_and_rejects_invalid_values(self):
        provider = LLMProvider(name='local', api_type='openai', base_url=' http://llm:8080/v1/ ')
        provider.clean()
        self.assertEqual(provider.base_url, 'http://llm:8080/v1')
        for url in ('file:///tmp/key', 'ftp://host/v1', 'https://user:secret@host/v1',
                    'https://host/v1?api_key=secret', 'https://host:invalid/v1'):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                LLMProvider(name='test', api_key='key', base_url=url).clean()

    def test_missing_key_or_disabled_provider_stop_before_sdk(self):
        for provider in (LLMProvider(name='test'), LLMProvider(name='test', is_active=False)):
            with self.subTest(provider=provider), self.assertRaises(LLMConfigurationError):
                create_chat_model(LLMModel(provider=provider, model='test-model'))

    def test_anthropic_temperature_is_validated_per_model(self):
        with self.assertRaises(ValidationError):
            LLMModel(provider=LLMProvider(name='test', api_key='key'), model='test', temperature=1.5).clean()


class LLMProtocolTests(SimpleTestCase):
    """拦截 HTTP 传输，验证实际 LangChain/SDK 的请求路径和响应解析，无外部 API 调用。"""

    def request(self, provider, response_data, *, base_url='https://gateway.example/prefix', **overrides):
        captured = []
        supplier = LLMProvider(name='Test supplier', api_type=provider, api_key='database-key',
                               base_url=base_url, max_retries=0, timeout=23)
        config = LLMModel(provider=supplier, model='gemini-2.5-flash' if provider == 'gemini' else 'test-model',
                          max_tokens=256, **overrides)

        def send(client, request, *args, **kwargs):
            captured.append(request)
            transport = importlib.import_module(type(request).__module__.split('.')[0])
            return transport.Response(200, json=response_data, request=request)

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false',
                'GOOGLE_GENAI_USE_VERTEXAI': 'false',
                'OPENAI_API_KEY': 'stale-env-key', 'ANTHROPIC_API_KEY': 'stale-env-key',
                'OPENAI_BASE_URL': 'https://stale.example', 'ANTHROPIC_BASE_URL': 'https://stale.example',
            }))
            stack.enter_context(patch.object(httpx.Client, 'send', new=send))
            # 当前 OpenAI / Anthropic SDK 使用 httpx2；同时兼容使用 httpx 的 SDK。
            try:
                import httpx2
            except ImportError:
                pass
            else:
                stack.enter_context(patch.object(httpx2.Client, 'send', new=send))
            result = call_llm_api('system prompt', 'user prompt', max_tokens=1024, temperature=0.4, model_config=config)
        self.assertEqual(len(captured), 1)
        self.assertEqual(extract_text_from_response(result), '{"ok":true}')
        return captured[0], json.loads(captured[0].content)

    def test_anthropic_custom_endpoint_and_native_response(self):
        request, body = self.request('anthropic', {
            'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'model': 'test-model',
            'content': [{'type': 'thinking', 'thinking': 'hidden', 'signature': 'test'},
                        {'type': 'text', 'text': '{"ok":true}'}],
            'stop_reason': 'end_turn', 'usage': {'input_tokens': 10, 'output_tokens': 5},
        })
        self.assertEqual(str(request.url), 'https://gateway.example/prefix/v1/messages')
        self.assertIn('database-key', (request.headers.get('x-api-key'),
                                      request.headers.get('authorization', '').removeprefix('Bearer ')))
        self.assertEqual(body['system'], 'system prompt')
        self.assertEqual(body['messages'][0]['content'], 'user prompt')
        self.assertEqual(body['max_tokens'], 256)
        self.assertEqual(body['temperature'], 0.4)
        self.assertEqual(request.extensions['timeout']['read'], 23)

    def test_chat_completions_stays_on_chat_protocol(self):
        request, body = self.request('openai', {
            'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 1, 'model': 'test-model',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': '{"ok":true}'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        }, temperature=0.2)
        self.assertEqual(str(request.url), 'https://gateway.example/prefix/chat/completions')
        self.assertEqual(request.headers['authorization'], 'Bearer database-key')
        self.assertEqual(body['messages'][0], {'role': 'system', 'content': 'system prompt'})
        self.assertEqual(body['messages'][1], {'role': 'user', 'content': 'user prompt'})
        self.assertEqual(body['max_completion_tokens'], 256)
        self.assertEqual(body['temperature'], 0.2)
        self.assertEqual(request.extensions['timeout']['read'], 23)

    def test_responses_uses_responses_endpoint_and_parses_output_blocks(self):
        request, body = self.request('openai_responses', {
            'id': 'resp_test', 'object': 'response', 'created_at': 1, 'model': 'test-model', 'status': 'completed',
            'output': [{'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': '{"ok":true}', 'annotations': []}]}],
            'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15,
                      'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}},
        })
        self.assertEqual(str(request.url), 'https://gateway.example/prefix/responses')
        self.assertIn('input', body)
        self.assertEqual(body['max_output_tokens'], 256)

    def test_gemini_custom_endpoint_and_native_response(self):
        request, body = self.request('gemini', {
            'candidates': [{'content': {'role': 'model', 'parts': [{'text': '{"ok":true}'}]},
                            'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 5, 'totalTokenCount': 15},
            'modelVersion': 'gemini-2.5-flash',
        })
        self.assertEqual(str(request.url), 'https://gateway.example/prefix/v1beta/models/gemini-2.5-flash:generateContent')
        self.assertEqual(request.headers['x-goog-api-key'], 'database-key')
        self.assertEqual(body['systemInstruction']['parts'][0]['text'], 'system prompt')
        self.assertEqual(body['contents'][0]['parts'][0]['text'], 'user prompt')
        self.assertEqual(body['generationConfig']['maxOutputTokens'], 256)
        self.assertEqual(request.extensions['timeout']['read'], 23)

    def test_official_default_endpoint_ignores_stale_environment_url(self):
        with patch.dict(os.environ, {'OPENAI_BASE_URL': 'https://stale.example',
                                     'OPENAI_API_BASE': 'https://stale.example'}):
            model = create_chat_model(LLMModel(model='test-model', provider=LLMProvider(name='test', api_type='openai', api_key='key')))
        self.assertEqual(str(model.root_client.base_url), 'https://api.openai.com/v1/')

    def test_gemini_retry_count_includes_initial_attempt(self):
        model = create_chat_model(LLMModel(model='gemini-2.5-flash', provider=LLMProvider(name='test', api_type='gemini', api_key='key', max_retries=0)))
        self.assertEqual(model.max_retries, 1)


class LLMResponseTests(SimpleTestCase):
    def test_plain_text_and_split_json_blocks(self):
        response = AIMessage(content=[
            {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'private reasoning'}]},
            {'type': 'text', 'text': '{"ok":'}, {'type': 'text', 'text': 'true}'},
        ])
        self.assertEqual(safe_json_loads(extract_text_from_response(response)), {'ok': True})
        self.assertEqual(extract_text_from_response(AIMessage(content='<think>secret</think>OK')), 'OK')
        self.assertEqual(safe_json_loads('[{"value": 1}]'), [{'value': 1}])

    def test_legacy_sdk_and_empty_response(self):
        legacy = SimpleNamespace(content=[SimpleNamespace(type='thinking', thinking='secret'),
                                          SimpleNamespace(type='text', text='OK')])
        self.assertEqual(extract_text_from_response(legacy), 'OK')
        self.assertIsNone(extract_text_from_response(AIMessage(content=[])))


class LLMAdminTests(TestCase):
    def setUp(self):
        reset_llm_catalog()
        self.provider = LLMProvider.objects.create(name='Supplier A', api_key='stored-secret')
        self.primary = LLMModel.objects.create(provider=self.provider, model='primary')
        self.backup = LLMModel.objects.create(provider=self.provider, model='backup')

    def form(self, **overrides):
        data = {'name': self.provider.name, 'api_type': 'anthropic', 'base_url': '', 'api_key': '',
                'timeout': 30, 'max_retries': 0, 'is_active': True}
        data.update(overrides)
        return LLMProviderAdminForm(data=data, instance=self.provider)

    def test_blank_password_keeps_key_without_rendering_it_into_html(self):
        self.assertNotIn('stored-secret', str(LLMProviderAdminForm(instance=self.provider)['api_key']))
        form = self.form(name='Renamed supplier')
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.api_key, 'stored-secret')
        self.assertEqual(self.provider.name, 'Renamed supplier')

    def test_key_can_be_rotated_or_explicitly_cleared(self):
        form = self.form(api_key='replacement-key')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().api_key, 'replacement-key')
        form = self.form(clear_api_key=True, is_active=False)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().api_key, '')

    def test_cannot_clear_required_key_while_enabled(self):
        form = self.form(clear_api_key=True)
        self.assertFalse(form.is_valid())
        self.assertIn('api_key', form.errors)

    def test_view_only_admin_does_not_see_the_stored_secret(self):
        from apps.users.models import User

        user = User.objects.create_user(email='viewer@example.com', is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename='view_llmprovider',
                                                       content_type__app_label='characters'))
        self.client.force_login(user)
        response = self.client.get(reverse('admin:characters_llmprovider_change', args=[self.provider.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'stored-secret')
        self.assertContains(response, '已配置')

    def test_admin_can_create_provider_and_multiple_models_in_one_form(self):
        from apps.users.models import User

        user = User.objects.create_superuser(username='admin', password='test', email='admin@example.com')
        self.client.force_login(user)
        data = {
            'name': 'New supplier', 'api_type': 'openai_responses', 'base_url': 'https://gateway.example/v1',
            'api_key': 'new-secret', 'timeout': 20, 'max_retries': 0, 'is_active': 'on',
            'models-TOTAL_FORMS': 2, 'models-INITIAL_FORMS': 0,
            'models-MIN_NUM_FORMS': 0, 'models-MAX_NUM_FORMS': 1000, '_save': 'Save',
        }
        for index, model in enumerate(('new-primary', 'new-backup')):
            data.update({
                f'models-{index}-model': model, f'models-{index}-display_name': '',
                f'models-{index}-is_active': 'on',
                f'models-{index}-max_tokens': 1024, f'models-{index}-temperature': '',
            })
        response = self.client.post(reverse('admin:characters_llmprovider_add'), data)
        self.assertEqual(response.status_code, 302)
        provider = LLMProvider.objects.get(name='New supplier')
        self.assertEqual(list(provider.models.order_by('pk').values_list('model', flat=True)), ['new-primary', 'new-backup'])
        self.assertEqual(LLMProvider.objects.count(), 2)

    def test_model_form_validates_supplier_specific_temperature(self):
        form_class = forms.modelform_factory(LLMModel, fields=['provider', 'model', 'temperature'])
        form = form_class(data={'provider': self.provider.pk, 'model': 'new-model', 'temperature': 1.5})
        self.assertFalse(form.is_valid())
        self.assertIn('temperature', form.errors)

    def test_provider_connection_action_only_tests_first_enabled_model(self):
        model_admin = LLMProviderAdmin(LLMProvider, AdminSite())
        with patch('apps.characters.services.llm_providers.call_llm_api') as invoke:
            with patch.object(model_admin, 'message_user') as messages:
                model_admin.test_connection(Mock(), [self.provider])
        self.assertEqual(invoke.call_args.kwargs['model_config'].pk, self.primary.pk)
        self.assertEqual(messages.call_args.kwargs['level'], 'success')

    def login_admin(self):
        from apps.users.models import User
        user = User.objects.create_superuser(username='admin', password='test', email='admin@example.com')
        self.client.force_login(user)
        return user

    def model_test_url(self, model=None):
        return reverse('admin:characters_llmprovider_model_test', args=[self.provider.pk, (model or self.backup).pk])

    def existing_provider_data(self):
        data = {
            'name': self.provider.name, 'api_type': self.provider.api_type, 'base_url': self.provider.base_url,
            'api_key': '', 'timeout': 30, 'max_retries': 0, 'is_active': 'on',
            'models-TOTAL_FORMS': 2, 'models-INITIAL_FORMS': 2,
            'models-MIN_NUM_FORMS': 0, 'models-MAX_NUM_FORMS': 1000, '_continue': 'Save',
        }
        for index, model in enumerate((self.primary, self.backup)):
            data.update({
                f'models-{index}-id': model.pk, f'models-{index}-model': model.model,
                f'models-{index}-display_name': '', f'models-{index}-is_active': 'on',
                f'models-{index}-max_tokens': 4096, f'models-{index}-temperature': '',
            })
        return data

    def test_models_have_no_separate_admin_entry(self):
        self.login_admin()
        self.assertFalse(admin.site.is_registered(LLMModel))
        page = self.client.get(reverse('admin:characters_llmprovider_change', args=[self.provider.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'data-provider-workspace')
        self.assertContains(page, '已配置的模型')
        self.assertContains(page, 'primary')
        self.assertContains(page, 'backup')
        self.assertNotContains(page, '/admin/characters/llmmodel/')

    def test_supplier_entry_opens_supplier_with_its_models(self):
        self.login_admin()
        response = self.client.get(reverse('admin:characters_llmprovider_changelist'))
        self.assertRedirects(response, reverse('admin:characters_llmprovider_change', args=[self.provider.pk]))

    def test_model_connection_endpoint_tests_selected_child_and_redacts_key(self):
        self.login_admin()
        with patch('apps.characters.services.llm_providers.call_llm_api', side_effect=ValueError('bad stored-secret')) as invoke:
            response = self.client.post(self.model_test_url())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['ok'])
        self.assertEqual(invoke.call_args.kwargs['model_config'].pk, self.backup.pk)
        self.assertNotIn('stored-secret', response.json()['message'])

    def test_model_connection_endpoint_reports_success(self):
        self.login_admin()
        with patch('apps.characters.services.llm_providers.call_llm_api', return_value=AIMessage(content='OK')) as invoke:
            response = self.client.post(self.model_test_url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(invoke.call_args.kwargs['max_retries'], 0)

    def test_connection_endpoint_rejects_model_from_different_supplier(self):
        self.login_admin()
        other_provider = LLMProvider.objects.create(name='Other', api_key='other-key')
        other_model = LLMModel.objects.create(provider=other_provider, model='other-model')
        with patch('apps.characters.services.llm_providers.call_llm_api') as invoke:
            response = self.client.post(self.model_test_url(other_model))
        self.assertEqual(response.status_code, 404)
        invoke.assert_not_called()

    def test_connection_endpoint_requires_post_and_supplier_change_permission(self):
        from apps.users.models import User
        viewer = User.objects.create_user(email='view-only@example.com', is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename='view_llmprovider',
                                                         content_type__app_label='characters'))
        self.client.force_login(viewer)
        with patch('apps.characters.services.llm_providers.call_llm_api') as invoke:
            self.assertEqual(self.client.get(self.model_test_url()).status_code, 405)
            self.assertEqual(self.client.post(self.model_test_url()).status_code, 403)
        invoke.assert_not_called()

    def test_supplier_permission_controls_editing_its_child_models(self):
        from apps.users.models import User
        editor = User.objects.create_user(email='supplier-editor@example.com', is_staff=True)
        editor.user_permissions.add(*Permission.objects.filter(
            codename__in=['view_llmprovider', 'change_llmprovider'], content_type__app_label='characters',
        ))
        self.client.force_login(editor)
        self.assertFalse(editor.has_perm('characters.change_llmmodel'))
        data = self.existing_provider_data()
        data['models-0-model'] = 'renamed-primary'
        response = self.client.post(reverse('admin:characters_llmprovider_change', args=[self.provider.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.primary.refresh_from_db()
        self.assertEqual(self.primary.model, 'renamed-primary')
        self.assertEqual(self.primary.provider_id, self.provider.pk)

    def test_unselected_model_can_be_deleted_inside_its_supplier(self):
        self.login_admin()
        data = self.existing_provider_data()
        data['models-1-DELETE'] = 'on'
        response = self.client.post(reverse('admin:characters_llmprovider_change', args=[self.provider.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LLMModel.objects.filter(pk=self.backup.pk).exists())
        self.assertTrue(LLMModel.objects.filter(pk=self.primary.pk).exists())

    def test_model_in_calling_chain_cannot_be_deleted_from_supplier(self):
        self.login_admin()
        configure_route(self.primary)
        data = self.existing_provider_data()
        data['models-0-DELETE'] = 'on'
        page = self.client.post(reverse('admin:characters_llmprovider_change', args=[self.provider.pk]), data)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '模型仍在调用链中使用')
        self.assertTrue(LLMModel.objects.filter(pk=self.primary.pk).exists())
        self.assertTrue(self.primary.routing_entries.exists())


class LLMRoutingAdminTests(TestCase):
    def setUp(self):
        from apps.users.models import User

        reset_llm_catalog()
        self.config = LLMRoutingConfig.objects.get(pk=1)
        a = LLMProvider.objects.create(name='A supplier', api_key='a-key')
        b = LLMProvider.objects.create(name='B supplier', api_key='b-key')
        self.first = LLMModel.objects.create(provider=a, model='first')
        self.second = LLMModel.objects.create(provider=a, model='second')
        self.third = LLMModel.objects.create(provider=b, model='third')
        user = User.objects.create_superuser(username='route-admin', password='test', email='route-admin@example.com')
        self.client.force_login(user)
        self.url = reverse('admin:characters_llmroutingconfig_change', args=[self.config.pk])

    def payload(self, models):
        return {'is_active': 'on', 'model_order': [model.pk for model in models], '_save': 'Save'}

    def test_route_page_selects_subset_in_the_order_added(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'A supplier: first')
        self.assertContains(page, 'A supplier: second')
        self.assertContains(page, 'B supplier: third')
        self.assertContains(page, 'characters/admin/llm_routing.js')
        self.assertContains(page, 'characters/admin/llm_routing.css')
        self.assertNotContains(page, 'name="entries-0-priority"')
        response = self.client.post(self.url, self.payload([self.third, self.first]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual([model.pk for model in get_available_models()], [self.third.pk, self.first.pk])
        self.assertEqual(list(self.config.entries.values_list('priority', flat=True)), [1, 2])
        self.assertFalse(self.second.routing_entries.exists())
        self.assertTrue(LLMModel.objects.get(pk=self.second.pk).is_active)

    def test_dropdown_disambiguates_model_ids_with_identical_display_names(self):
        LLMModel.objects.filter(pk__in=[self.first.pk, self.second.pk]).update(display_name='Same alias')
        page = self.client.get(self.url)
        self.assertContains(page, 'A supplier: first（Same alias）')
        self.assertContains(page, 'A supplier: second（Same alias）')

    def test_duplicate_model_selection_is_rejected_without_saving_entries(self):
        response = self.client.post(self.url, self.payload([self.first, self.first]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.config.entries.exists())

    def test_removing_model_from_list_preserves_model_configuration(self):
        configure_route(self.first, self.third)
        response = self.client.post(self.url, self.payload([self.first]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual([model.pk for model in get_available_models()], [self.first.pk])
        self.assertTrue(LLMModel.objects.filter(pk=self.third.pk).exists())

    def test_reordering_without_changing_selection_is_recorded_and_saved(self):
        configure_route(self.first, self.third)
        form = LLMRoutingConfigAdminForm(data=self.payload([self.third, self.first]), instance=self.config)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn('model_order', form.changed_data)
        self.assertEqual([model.pk for model in form.cleaned_data['model_order']], [self.third.pk, self.first.pk])
        form.save()
        self.assertEqual([model.pk for model in get_available_models()], [self.third.pk, self.first.pk])
        reloaded = LLMRoutingConfigAdminForm(instance=self.config)
        self.assertEqual(reloaded.initial['model_order'], [self.third.pk, self.first.pk])

    def test_invalid_model_does_not_replace_the_current_chain(self):
        configure_route(self.first, self.third)
        data = self.payload([self.first])
        data['model_order'].append(999999)
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([model.pk for model in get_available_models()], [self.first.pk, self.third.pk])

    def test_removing_all_items_keeps_the_model_catalog(self):
        configure_route(self.first, self.third)
        response = self.client.post(self.url, self.payload([]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.config.entries.exists())
        self.assertEqual(LLMModel.objects.count(), 3)

    def test_view_only_user_sees_saved_list_without_editing_controls(self):
        from apps.users.models import User

        configure_route(self.third, self.first)
        viewer = User.objects.create_user(email='routing-viewer@example.com', is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename='view_llmroutingconfig',
                                                         content_type__app_label='characters'))
        self.client.force_login(viewer)
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '#1 首选')
        self.assertNotContains(page, 'data-llm-routing-widget')

    def test_supplier_page_no_longer_exposes_model_priority(self):
        response = self.client.get(reverse('admin:characters_llmprovider_change', args=[self.first.provider_id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="models-0-priority"')


class LLMWorkflowTests(TestCase):
    @override_settings(ANTHROPIC_API_KEY='')
    def test_report_analysis_no_longer_requires_anthropic_environment_key(self):
        from apps.characters.services.llm_service import analyze_module_structured

        with patch('apps.characters.services.llm_utils.call_llm_api', return_value=AIMessage(content='{"ok":true}')) as invoke:
            with redirect_stdout(StringIO()):
                result = analyze_module_structured('title_summary', {}, 'Test', {})
        self.assertEqual(result, {'ok': True})
        self.assertIs(invoke.call_args.kwargs['response_validator'], validate_json_object)

    @override_settings(ANTHROPIC_API_KEY='')
    def test_persona_history_records_actual_fallback_model(self):
        from apps.characters.models import Character, DailyReport, DailyReportConfig, PersonaHistory
        from apps.characters.services.persona_service import update_system_persona
        from apps.users.models import User

        reset_llm_catalog()
        provider = LLMProvider.objects.create(name='A', api_key='db-key')
        first = LLMModel.objects.create(provider=provider, model='primary')
        fallback_name = 'fallback-model-' + 'x' * 120
        second = LLMModel.objects.create(provider=provider, model=fallback_name)
        configure_route(first, second)
        user = User.objects.create_user(email='persona@example.com')
        character = Character.objects.create(user=user, name='Test')
        config = DailyReportConfig.objects.create(character=character)
        today = timezone.localdate()
        for days_ago in range(1, 4):
            DailyReport.objects.create(character=character, date=today - timedelta(days=days_ago),
                                       raw_data={'total_records': 10})
        primary, backup = Mock(), Mock()
        primary.invoke.side_effect = TimeoutError('timeout')
        backup.invoke.return_value = AIMessage(content='Updated persona')
        with patch('apps.characters.services.llm_providers.create_chat_model', side_effect=[primary, backup]):
            with redirect_stdout(StringIO()):
                update_system_persona(config, today=today)
        config.refresh_from_db()
        self.assertEqual(config.system_inferred_persona, 'Updated persona')
        self.assertEqual(PersonaHistory.objects.get(config=config, date=today).model_used, fallback_name)

    def test_real_sdk_request_falls_back_between_protocols_without_external_network(self):
        reset_llm_catalog()
        a = LLMProvider.objects.create(name='OpenAI gateway', api_type='openai', api_key='a-key',
                                       base_url='https://primary.example/v1', timeout=5, max_retries=0)
        b = LLMProvider.objects.create(name='Anthropic gateway', api_type='anthropic', api_key='b-key',
                                       base_url='https://backup.example', timeout=9, max_retries=0)
        first = LLMModel.objects.create(provider=a, model='primary')
        backup = LLMModel.objects.create(provider=b, model='backup')
        configure_route(first, backup)
        captured = []

        def send(client, request, *args, **kwargs):
            captured.append(request)
            transport = importlib.import_module(type(request).__module__.split('.')[0])
            if request.url.host == 'primary.example':
                return transport.Response(503, json={'error': {'message': 'unavailable', 'type': 'server_error'}}, request=request)
            return transport.Response(200, json={
                'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'model': 'backup',
                'content': [{'type': 'text', 'text': 'OK'}], 'stop_reason': 'end_turn',
                'usage': {'input_tokens': 10, 'output_tokens': 5},
            }, request=request)

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false'}))
            stack.enter_context(patch.object(httpx.Client, 'send', new=send))
            try:
                import httpx2
            except ImportError:
                pass
            else:
                stack.enter_context(patch.object(httpx2.Client, 'send', new=send))
            result = call_llm_api('system', 'user')
        self.assertEqual([str(request.url) for request in captured],
                         ['https://primary.example/v1/chat/completions', 'https://backup.example/v1/messages'])
        self.assertEqual([request.extensions['timeout']['read'] for request in captured], [5, 9])
        self.assertEqual(result.response_metadata['llm_model_id'], backup.pk)
        self.assertEqual(result.response_metadata['llm_fallback_count'], 1)


class LLMConfigMigrationTests(TransactionTestCase):
    def test_split_preserves_existing_credentials_generation_settings_and_disabled_state(self):
        old_target = [('characters', '0025_llmconfig')]
        new_target = [('characters', '0026_llm_provider_models')]
        executor = MigrationExecutor(connection)
        executor.migrate(old_target)
        try:
            old_apps = executor.loader.project_state(old_target).apps
            legacy = old_apps.get_model('characters', 'LLMConfig')
            legacy.objects.update_or_create(pk=1, defaults={
                'provider': 'openai_responses', 'model': 'existing-model', 'api_key': 'existing-key',
                'base_url': 'https://existing.example/v1', 'timeout': 42, 'max_retries': 2,
                'max_tokens': 2048, 'temperature': 0.3, 'is_active': False,
            })
            executor = MigrationExecutor(connection)
            executor.migrate(new_target)
            new_apps = executor.loader.project_state(new_target).apps
            supplier = new_apps.get_model('characters', 'LLMProvider').objects.get(name='原 LLM 配置')
            model = new_apps.get_model('characters', 'LLMModel').objects.get(provider_id=supplier.pk)
            self.assertEqual((supplier.api_type, supplier.api_key, supplier.base_url),
                             ('openai_responses', 'existing-key', 'https://existing.example/v1'))
            self.assertEqual((supplier.timeout, supplier.max_retries, supplier.is_active), (42, 2, False))
            self.assertEqual((model.model, model.max_tokens, model.temperature, model.priority),
                             ('existing-model', 2048, 0.3, 10))
        finally:
            MigrationExecutor(connection).migrate([('characters', '0027_llm_routing')])

    def test_routing_migration_preserves_existing_order_and_excludes_disabled_models(self):
        old_target = [('characters', '0026_llm_provider_models')]
        new_target = [('characters', '0027_llm_routing')]
        executor = MigrationExecutor(connection)
        executor.migrate(old_target)
        try:
            old_apps = executor.loader.project_state(old_target).apps
            supplier_class = old_apps.get_model('characters', 'LLMProvider')
            model_class = old_apps.get_model('characters', 'LLMModel')
            supplier_class.objects.all().delete()
            enabled = supplier_class.objects.create(name='Enabled', api_key='key')
            disabled = supplier_class.objects.create(name='Disabled', api_key='key', is_active=False)
            later = model_class.objects.create(provider_id=enabled.pk, model='later', priority=30)
            first = model_class.objects.create(provider_id=enabled.pk, model='first', priority=10)
            model_class.objects.create(provider_id=enabled.pk, model='disabled-model', priority=0, is_active=False)
            model_class.objects.create(provider_id=disabled.pk, model='disabled-supplier-model', priority=0)
            executor = MigrationExecutor(connection)
            executor.migrate(new_target)
            new_apps = executor.loader.project_state(new_target).apps
            config = new_apps.get_model('characters', 'LLMRoutingConfig').objects.get(pk=1)
            entry_class = new_apps.get_model('characters', 'LLMRoutingEntry')
            entries = entry_class.objects.filter(config_id=config.pk).order_by('priority', 'pk')
            self.assertEqual(list(entries.values_list('model_id', 'priority')), [(first.pk, 10), (later.pk, 30)])
            new_model_class = new_apps.get_model('characters', 'LLMModel')
            added = new_model_class.objects.create(provider_id=enabled.pk, model='added-after-upgrade')
            self.assertFalse(entry_class.objects.filter(model_id=added.pk).exists())
            self.assertNotIn('priority', [field.name for field in new_model_class._meta.fields])
        finally:
            MigrationExecutor(connection).migrate(new_target)
