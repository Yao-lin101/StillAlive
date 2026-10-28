import importlib
import os
from contextlib import ExitStack
from unittest.mock import patch

import httpx
from django.contrib.auth.models import Permission
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from apps.characters.models import LLMModel, LLMProvider, LLMRoutingEntry
from apps.characters.services.llm_model_catalog import LLMModelDiscoveryError, fetch_provider_models
from apps.characters.tests.test_llm_providers import reset_llm_catalog
from apps.users.models import User


class LLMModelCatalogProtocolTests(SimpleTestCase):
    """使用实际供应商 SDK，拦截传输验证模型列表协议及分页，无真实 API 调用。"""

    def discover(self, api_type, pages, *, base_url='https://gateway.example/prefix', api_key='database-key'):
        provider = LLMProvider(name='Supplier', api_type=api_type, api_key=api_key,
                               base_url=base_url, timeout=17, max_retries=0)
        requests = []

        def send(client, request, *args, **kwargs):
            requests.append(request)
            payload = pages[len(requests) - 1]
            status, body = payload if isinstance(payload, tuple) else (200, payload)
            transport = importlib.import_module(type(request).__module__.split('.')[0])
            return transport.Response(status, json=body, request=request)

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                'GOOGLE_GENAI_USE_VERTEXAI': 'true', 'GOOGLE_API_KEY': 'environment-secret',
                'OPENAI_API_KEY': 'environment-secret', 'ANTHROPIC_API_KEY': 'environment-secret',
                'OPENAI_BASE_URL': 'https://stale.example', 'ANTHROPIC_BASE_URL': 'https://stale.example',
            }))
            stack.enter_context(patch.object(httpx.Client, 'send', new=send))
            try:
                import httpx2
            except ImportError:
                pass
            else:
                stack.enter_context(patch.object(httpx2.Client, 'send', new=send))
            result = fetch_provider_models(provider)
        return result, requests

    def test_openai_and_responses_use_models_endpoint_with_custom_prefix(self):
        for api_type in ('openai', 'openai_responses'):
            with self.subTest(api_type=api_type):
                result, requests = self.discover(api_type, [{'data': [
                    {'id': 'z-model', 'object': 'model', 'created': 1, 'owned_by': 'vendor'},
                    {'id': 'a-model', 'object': 'model', 'created': 1, 'owned_by': 'vendor'},
                    {'id': 'a-model', 'object': 'model', 'created': 1, 'owned_by': 'vendor'},
                ], 'object': 'list'}])
                self.assertEqual([item['id'] for item in result], ['a-model', 'z-model'])
                self.assertEqual(str(requests[0].url), 'https://gateway.example/prefix/models')
                self.assertEqual(requests[0].method, 'GET')
                self.assertEqual(requests[0].headers['authorization'], 'Bearer database-key')
                self.assertEqual(requests[0].extensions['timeout']['read'], 17)

    def test_openai_custom_endpoint_supports_no_auth_without_inheriting_environment_key(self):
        result, requests = self.discover('openai', [{'data': [], 'object': 'list'}], api_key='')
        self.assertEqual(result, [])
        self.assertNotIn('environment-secret', str(requests[0].headers))

    def test_anthropic_paginates_and_keeps_display_names(self):
        result, requests = self.discover('anthropic', [
            {'data': [{'id': 'claude-a', 'display_name': 'Claude A', 'type': 'model',
                       'created_at': '2025-01-01T00:00:00Z'}],
             'has_more': True, 'last_id': 'claude-a', 'first_id': 'claude-a'},
            {'data': [{'id': 'claude-b', 'display_name': 'Claude B', 'type': 'model',
                       'created_at': '2025-01-01T00:00:00Z'}],
             'has_more': False, 'last_id': 'claude-b', 'first_id': 'claude-b'},
        ])
        self.assertEqual(result, [{'id': 'claude-a', 'display_name': 'Claude A'},
                                  {'id': 'claude-b', 'display_name': 'Claude B'}])
        self.assertEqual(requests[0].url.path, '/prefix/v1/models')
        self.assertIn('database-key', (requests[0].headers.get('x-api-key'),
                                      requests[0].headers.get('authorization', '').removeprefix('Bearer ')))
        self.assertEqual(requests[1].url.params['after_id'], 'claude-a')
        self.assertEqual(len(requests), 2)

    def test_gemini_paginates_normalizes_ids_and_filters_embedding_models(self):
        result, requests = self.discover('gemini', [
            {'models': [
                {'name': 'models/gemini-a', 'displayName': 'Gemini A',
                 'supportedGenerationMethods': ['generateContent', 'countTokens']},
                {'name': 'models/embedding-a', 'supportedGenerationMethods': ['embedContent']},
            ], 'nextPageToken': 'next-page'},
            {'models': [
                {'name': 'models/gemini-b', 'displayName': 'Gemini B',
                 'supportedGenerationMethods': ['generateContent']},
            ]},
        ])
        self.assertEqual(result, [{'id': 'gemini-a', 'display_name': 'Gemini A'},
                                  {'id': 'gemini-b', 'display_name': 'Gemini B'}])
        self.assertEqual(requests[0].url.path, '/prefix/v1beta/models')
        self.assertEqual(requests[0].headers['x-goog-api-key'], 'database-key')
        self.assertEqual(requests[1].url.params['pageToken'], 'next-page')
        self.assertEqual(requests[0].extensions['timeout']['read'], 17)
        self.assertEqual(len(requests), 2)

    def test_default_endpoints_ignore_environment_urls(self):
        for api_type, endpoint, payload in (
            ('openai', 'https://api.openai.com/v1/models', {'data': []}),
            ('openai_responses', 'https://api.openai.com/v1/models', {'data': []}),
            ('anthropic', 'https://api.anthropic.com/v1/models', {'data': [], 'has_more': False}),
            ('gemini', 'https://generativelanguage.googleapis.com/v1beta/models', {'models': []}),
        ):
            with self.subTest(api_type=api_type):
                result, requests = self.discover(api_type, [payload], base_url='')
                self.assertEqual(result, [])
                self.assertEqual(str(requests[0].url).split('?')[0], endpoint)

    def test_upstream_errors_are_useful_and_do_not_echo_secrets(self):
        for status, message in ((401, '鉴权失败'), (404, '模型列表接口'), (429, '过于频繁'), (500, '获取模型列表失败')):
            with self.subTest(status=status):
                with self.assertRaises(LLMModelDiscoveryError) as caught:
                    self.discover('openai', [(status, {'error': {'message': 'database-key environment-secret'}})])
                self.assertIn(message, str(caught.exception))
                self.assertNotIn('database-key', str(caught.exception))
                self.assertNotIn('environment-secret', str(caught.exception))

    def test_invalid_model_ids_are_rejected(self):
        with self.assertRaisesMessage(LLMModelDiscoveryError, '无效的模型 ID'):
            self.discover('openai', [{'data': [{'id': ''}]}])

    def test_malformed_list_is_reported_instead_of_treated_as_empty(self):
        with self.assertRaisesMessage(LLMModelDiscoveryError, '无效的模型列表'):
            self.discover('openai', [{'unexpected': []}])

    def test_later_page_failure_does_not_return_partial_catalog(self):
        with self.assertRaisesMessage(LLMModelDiscoveryError, '鉴权失败'):
            self.discover('anthropic', [
                {'data': [{'id': 'first', 'display_name': 'First', 'type': 'model'}],
                 'has_more': True, 'last_id': 'first'},
                (401, {'error': {'type': 'authentication_error', 'message': 'database-key'}}),
            ])

    def test_disabled_provider_can_be_discovered_but_still_needs_credentials(self):
        provider = LLMProvider(name='Disabled', api_type='gemini', is_active=False)
        with self.assertRaisesMessage(LLMModelDiscoveryError, '请填写 API Key'):
            fetch_provider_models(provider)


class LLMModelCatalogAdminTests(TestCase):
    def setUp(self):
        reset_llm_catalog()
        self.provider = LLMProvider.objects.create(name='Saved supplier', api_type='openai',
                                                   api_key='saved-secret', base_url='https://saved.example/v1')
        self.model = LLMModel.objects.create(provider=self.provider, model='configured-model')
        self.user = User.objects.create_superuser(username='catalog-admin', email='catalog@example.test', password='test')
        self.client.force_login(self.user)
        self.url = reverse('admin:characters_llmprovider_discover_models', args=[self.provider.pk])
        self.add_url = reverse('admin:characters_llmprovider_discover_models_add')

    def data(self, **overrides):
        data = {'api_type': 'openai', 'base_url': self.provider.base_url, 'api_key': '',
                'timeout': 20, 'max_retries': 0}
        data.update(overrides)
        return data

    def test_discovery_returns_temporary_list_without_saving_or_changing_routing(self):
        with patch('apps.characters.services.llm_model_catalog.fetch_provider_models',
                   return_value=[{'id': 'new-model', 'display_name': 'New model'}]) as discover:
            response = self.client.post(self.url, self.data())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['models'], [{'id': 'new-model', 'display_name': 'New model'}])
        self.assertEqual(discover.call_args.args[0].api_key, 'saved-secret')
        self.assertEqual(LLMModel.objects.count(), 1)
        self.assertEqual(LLMRoutingEntry.objects.count(), 0)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.timeout, 180)
        self.assertNotContains(response, 'saved-secret')

    def test_unsaved_connection_changes_are_used_without_overwriting_provider(self):
        with patch('apps.characters.services.llm_model_catalog.fetch_provider_models', return_value=[]) as discover:
            response = self.client.post(self.url, self.data(api_type='gemini', api_key='draft-secret',
                                                            base_url='https://draft.example/prefix'))
        self.assertEqual(response.status_code, 200)
        supplier = discover.call_args.args[0]
        self.assertEqual((supplier.api_type, supplier.api_key, supplier.base_url),
                         ('gemini', 'draft-secret', 'https://draft.example/prefix'))
        self.provider.refresh_from_db()
        self.assertEqual((self.provider.api_type, self.provider.api_key, self.provider.base_url),
                         ('openai', 'saved-secret', 'https://saved.example/v1'))

    def test_new_provider_can_discover_before_name_or_provider_has_been_saved(self):
        with patch('apps.characters.services.llm_model_catalog.fetch_provider_models', return_value=[]) as discover:
            response = self.client.post(self.add_url, self.data(api_key='new-secret'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(discover.call_args.args[0].pk)
        self.assertEqual(discover.call_args.args[0].api_key, 'new-secret')
        self.assertEqual(LLMProvider.objects.count(), 1)

    def test_key_can_be_explicitly_cleared_for_no_auth_gateway(self):
        with patch('apps.characters.services.llm_model_catalog.fetch_provider_models', return_value=[]) as discover:
            response = self.client.post(self.url, self.data(clear_api_key='on'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(discover.call_args.args[0].api_key, '')
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.api_key, 'saved-secret')

    def test_invalid_connection_configuration_never_contacts_upstream(self):
        for data in (self.data(base_url='file:///etc/passwd'), self.data(timeout=0),
                     self.data(api_type='unknown'), self.data(api_type='anthropic', clear_api_key='on')):
            with self.subTest(data=data), patch('apps.characters.services.llm_model_catalog.fetch_provider_models') as discover:
                response = self.client.post(self.url, data)
                self.assertEqual(response.status_code, 400)
                self.assertIn('errors', response.json())
                discover.assert_not_called()

    def test_discovery_error_redacts_both_saved_and_draft_keys(self):
        with patch('apps.characters.services.llm_model_catalog.fetch_provider_models',
                   side_effect=LLMModelDiscoveryError('bad saved-secret and draft-secret')):
            response = self.client.post(self.url, self.data(api_key='draft-secret'))
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('saved-secret', response.json()['message'])
        self.assertNotIn('draft-secret', response.json()['message'])

    def test_discovery_requires_post_and_change_permission(self):
        viewer = User.objects.create_user(email='catalog-viewer@example.test', is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename='view_llmprovider',
                                                           content_type__app_label='characters'))
        self.client.force_login(viewer)
        with patch('apps.characters.services.llm_model_catalog.fetch_provider_models') as discover:
            self.assertEqual(self.client.get(self.url).status_code, 405)
            self.assertEqual(self.client.post(self.url, self.data()).status_code, 403)
            self.assertEqual(self.client.post(self.add_url, self.data()).status_code, 403)
            discover.assert_not_called()
        page = self.client.get(reverse('admin:characters_llmprovider_change', args=[self.provider.pk]))
        self.assertNotContains(page, 'data-fetch-models=')

    def test_redaction_handles_new_key_containing_the_old_key(self):
        with patch('apps.characters.services.llm_model_catalog.fetch_provider_models',
                   side_effect=LLMModelDiscoveryError('bad saved-secret-extended')):
            response = self.client.post(self.url, self.data(api_key='saved-secret-extended'))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['message'], 'bad ***')

    def test_discovery_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        with patch('apps.characters.services.llm_model_catalog.fetch_provider_models') as discover:
            self.assertEqual(client.post(self.url, self.data()).status_code, 403)
            discover.assert_not_called()

    def test_provider_editor_does_not_need_separate_model_permissions(self):
        editor = User.objects.create_user(email='catalog-editor@example.test', is_staff=True)
        editor.user_permissions.add(Permission.objects.get(codename='change_llmprovider',
                                                           content_type__app_label='characters'))
        self.client.force_login(editor)
        with patch('apps.characters.services.llm_model_catalog.fetch_provider_models', return_value=[]):
            self.assertEqual(self.client.post(self.url, self.data()).status_code, 200)

    def test_add_only_admin_can_discover_new_provider_but_not_existing_provider(self):
        creator = User.objects.create_user(email='catalog-creator@example.test', is_staff=True)
        creator.user_permissions.add(Permission.objects.get(codename='add_llmprovider',
                                                            content_type__app_label='characters'))
        self.client.force_login(creator)
        with patch('apps.characters.services.llm_model_catalog.fetch_provider_models', return_value=[]) as discover:
            self.assertEqual(self.client.post(self.add_url, self.data(api_key='new-key')).status_code, 200)
            self.assertEqual(self.client.post(self.url, self.data()).status_code, 403)
            self.assertEqual(discover.call_count, 1)
        page = self.client.get(reverse('admin:characters_llmprovider_add'))
        self.assertContains(page, 'data-fetch-models=')
        self.assertContains(page, 'name="api_key"')

    def test_discovery_controls_are_inside_provider_model_editor(self):
        for url in (reverse('admin:characters_llmprovider_change', args=[self.provider.pk]),
                    reverse('admin:characters_llmprovider_add')):
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertContains(page, '获取模型列表')
                self.assertContains(page, 'data-model-discovery')
                self.assertContains(page, '添加所选模型')
                self.assertContains(page, '点击“保存配置”才会保存')
                self.assertNotContains(page, 'saved-secret')
