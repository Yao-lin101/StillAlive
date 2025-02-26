from django.test import TestCase
from django.urls import reverse
from django.core import mail
from django.core.cache import cache
from rest_framework.test import APITestCase
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User

class UserRegistrationTests(APITestCase):
    """用户注册相关测试"""
    
    def setUp(self):
        """测试前准备工作"""
        self.register_url = reverse('user-register-email')
        self.verify_code_url = reverse('user-send-verify-code')
        self.valid_payload = {
            'email': 'test@example.com',
            'password': 'testpass123',
            'verify_code': '123456'
        }
        # 清除缓存
        cache.clear()

    def test_send_verify_code(self):
        """测试发送验证码"""
        data = {'email': 'test@example.com'}
        response = self.client.post(self.verify_code_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['test@example.com'])

    def test_register_with_valid_data(self):
        """测试使用有效数据注册"""
        # 模拟验证码已发送
        cache.set('email_verify_code_test@example.com', '123456', timeout=300)
        
        response = self.client.post(self.register_url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue('user' in response.data)
        self.assertTrue('token' in response.data)
        self.assertEqual(response.data['user']['email'], 'test@example.com')
        self.assertTrue(User.objects.filter(email='test@example.com').exists())

    def test_register_with_invalid_verify_code(self):
        """测试使用无效验证码注册"""
        self.valid_payload['verify_code'] = '000000'
        response = self.client.post(self.register_url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_with_existing_email(self):
        """测试使用已存在的邮箱注册"""
        # 先创建一个用户
        User.objects.create_user(email='test@example.com', password='testpass123')
        
        # 模拟验证码已发送
        cache.set('email_verify_code_test@example.com', '123456', timeout=300)
        
        response = self.client.post(self.register_url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

class UserProfileTests(APITestCase):
    """用户资料相关测试"""
    
    def setUp(self):
        """测试前准备工作"""
        self.user = User.objects.create_user(
            email='test@example.com',
            password='testpass123',
            username='testuser'
        )
        self.profile_url = reverse('user-profile')
        self.client.force_authenticate(user=self.user)

    def test_get_profile(self):
        """测试获取用户资料"""
        response = self.client.get(self.profile_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['email'], 'test@example.com')
        self.assertEqual(response.data['username'], 'testuser')

    def test_update_profile(self):
        """测试更新用户资料"""
        data = {'username': 'newusername'}
        response = self.client.put(self.profile_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], 'newusername')
        
    def test_update_profile_without_auth(self):
        """测试未认证时更新资料"""
        self.client.force_authenticate(user=None)
        data = {'username': 'newusername'}
        response = self.client.put(self.profile_url, data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

class PasswordChangeTests(APITestCase):
    """密码修改相关测试"""
    
    def setUp(self):
        """测试前准备工作"""
        self.user = User.objects.create_user(
            email='test@example.com',
            password='oldpass123'
        )
        self.change_password_url = reverse('user-change-password')
        self.client.force_authenticate(user=self.user)

    def test_change_password(self):
        """测试修改密码"""
        data = {
            'old_password': 'oldpass123',
            'new_password': 'newpass123'
        }
        response = self.client.post(self.change_password_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # 验证新密码是否生效
        self.assertTrue(
            User.objects.get(email='test@example.com').check_password('newpass123')
        )

    def test_change_password_with_wrong_old_password(self):
        """测试使用错误的旧密码"""
        data = {
            'old_password': 'wrongpass',
            'new_password': 'newpass123'
        }
        response = self.client.post(self.change_password_url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

class TokenTests(APITestCase):
    """Token相关测试"""
    
    def setUp(self):
        """测试前准备工作"""
        self.user = User.objects.create_user(
            email='test@example.com',
            password='testpass123'
        )
        self.token_url = reverse('token_obtain_pair')
        self.refresh_url = reverse('token_refresh')

    def test_obtain_token(self):
        """测试获取token"""
        data = {
            'email': 'test@example.com',
            'password': 'testpass123'
        }
        response = self.client.post(self.token_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('access' in response.data)
        self.assertTrue('refresh' in response.data)

    def test_obtain_token_with_invalid_credentials(self):
        """测试使用无效凭据获取token"""
        data = {
            'email': 'test@example.com',
            'password': 'wrongpass'
        }
        response = self.client.post(self.token_url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('detail' in response.data)

    def test_refresh_token(self):
        """测试刷新token"""
        # 先获取refresh token
        refresh = RefreshToken.for_user(self.user)
        
        data = {'refresh': str(refresh)}
        response = self.client.post(self.refresh_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('access' in response.data)
