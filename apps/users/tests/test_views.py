from django.test import TestCase
from django.urls import reverse
from django.core.cache import cache
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from rest_framework import status
import re

from django.contrib.auth import get_user_model
User = get_user_model()

class UserViewSetTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='test@example.com',
            password='testpass123',
            is_email_verified=True
        )
        self.client.force_authenticate(user=self.user)

    def test_register_email(self):
        """测试邮箱注册"""
        # 发送验证码
        response = self.client.post('/api/v1/users/send-verify-code/', {
            'email': 'new@example.com'
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # 从缓存中获取验证码
        cache_key = f'email_verify_code_new@example.com'
        verify_code = cache.get(cache_key)
        self.assertIsNotNone(verify_code)
        
        # 注册
        response = self.client.post('/api/v1/users/register-email/', {
            'email': 'new@example.com',
            'password': 'newpass123',
            'verify_code': verify_code
        })
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(email='new@example.com').exists())
        self.assertIn('refresh', response.data)
        self.assertIn('access', response.data)
        self.assertIn('user', response.data)

    def test_send_verify_code_rate_limit(self):
        """测试验证码发送频率限制"""
        email = 'test2@example.com'  # 使用一个新的邮箱地址
        
        # 第一次发送
        response = self.client.post('/api/v1/users/send-verify-code/', {
            'email': email
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # 立即再次发送
        response = self.client.post('/api/v1/users/send-verify-code/', {
            'email': email
        })
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_profile_get(self):
        """测试获取用户资料"""
        response = self.client.get('/api/v1/users/profile/')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['email'], self.user.email)
        self.assertTrue(response.data['uid'].startswith('smtx'))

    def test_profile_update(self):
        """测试更新用户资料"""
        response = self.client.put(
            '/api/v1/users/profile/',
            {'username': 'newusername'}
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], 'newusername')
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'newusername')

    def test_change_password(self):
        """测试修改密码"""
        response = self.client.post(
            reverse('user-change-password'),
            {
                'old_password': 'testpass123',
                'new_password': 'newtestpass123'
            }
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('newtestpass123'))

    def test_change_password_wrong_old_password(self):
        """测试使用错误的旧密码修改密码"""
        response = self.client.post(
            reverse('user-change-password'),
            {
                'old_password': 'wrongpass',
                'new_password': 'newtestpass123'
            }
        )
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('testpass123'))

    def test_upload_avatar(self):
        """测试上传头像"""
        # 创建一个测试图片
        image_data = b'fake-image-data'
        avatar = SimpleUploadedFile(
            name='test.jpg',
            content=image_data,
            content_type='image/jpeg'
        )
        
        # 上传头像
        response = self.client.post(
            reverse('user-upload-avatar'),
            {'avatar': avatar},
            format='multipart'
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data['avatar'])
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.avatar)
        
    def test_upload_avatar_without_auth(self):
        """测试未认证时上传头像"""
        self.client.force_authenticate(user=None)
        
        image_data = b'fake-image-data'
        avatar = SimpleUploadedFile(
            name='test.jpg',
            content=image_data,
            content_type='image/jpeg'
        )
        
        response = self.client.post(
            reverse('user-upload-avatar'),
            {'avatar': avatar},
            format='multipart'
        )
        
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def tearDown(self):
        cache.clear()
        mail.outbox = [] 