from django.test import TestCase
from django.contrib.auth import get_user_model, authenticate
from apps.users.backends import EmailBackend

User = get_user_model()

class EmailBackendTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.backend = EmailBackend()

    def test_authenticate_with_email(self):
        """测试使用邮箱登录"""
        user = authenticate(
            username='test@example.com',
            password='testpass123'
        )
        self.assertIsNotNone(user)
        self.assertEqual(user.email, 'test@example.com')

    def test_authenticate_with_username(self):
        """测试使用用户名登录"""
        user = authenticate(
            username='testuser',
            password='testpass123'
        )
        self.assertIsNotNone(user)
        self.assertEqual(user.username, 'testuser')

    def test_authenticate_with_wrong_password(self):
        """测试密码错误"""
        user = authenticate(
            username='test@example.com',
            password='wrongpass'
        )
        self.assertIsNone(user)

    def test_authenticate_with_nonexistent_user(self):
        """测试不存在的用户"""
        user = authenticate(
            username='nonexistent@example.com',
            password='testpass123'
        )
        self.assertIsNone(user)

    def test_get_user(self):
        """测试获取用户"""
        user = self.backend.get_user(self.user.pk)
        self.assertEqual(user, self.user)

    def test_get_nonexistent_user(self):
        """测试获取不存在的用户"""
        user = self.backend.get_user(999)
        self.assertIsNone(user) 