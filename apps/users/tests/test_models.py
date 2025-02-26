from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

User = get_user_model()

class UserModelTests(TestCase):
    def setUp(self):
        self.user_data = {
            'email': 'test@example.com',
            'password': 'testpass123',
        }

    def test_create_user(self):
        """测试创建普通用户"""
        user = User.objects.create_user(**self.user_data)
        
        self.assertEqual(user.email, self.user_data['email'])
        self.assertTrue(user.check_password(self.user_data['password']))
        self.assertTrue(user.uid.startswith('smtx'))
        self.assertEqual(len(user.uid), 14)  # smtx + 10位数字
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.username, user.uid)

    def test_create_superuser(self):
        """测试创建超级用户"""
        admin_user = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='admin123'
        )
        
        self.assertTrue(admin_user.is_staff)
        self.assertTrue(admin_user.is_superuser)
        self.assertTrue(admin_user.uid.startswith('smtx'))

    def test_user_email_unique(self):
        """测试邮箱唯一性"""
        User.objects.create_user(**self.user_data)
        
        user2 = User(
            email=self.user_data['email'],
            password='anotherpass123'
        )
        with self.assertRaises(ValidationError):
            user2.full_clean()

    def test_user_without_email_and_wechat(self):
        """测试创建用户时必须提供邮箱或微信ID"""
        with self.assertRaises(ValueError):
            User.objects.create_user(password='testpass123')

    def test_uid_generation(self):
        """测试UID生成规则"""
        users = [User.objects.create_user(
            email=f'test{i}@example.com',
            password='testpass123'
        ) for i in range(5)]
        
        # 测试UID格式
        for user in users:
            self.assertTrue(user.uid.startswith('smtx'))
            self.assertEqual(len(user.uid), 14)
            self.assertTrue(user.uid[4:].isdigit())
        
        # 测试UID唯一性
        uids = [user.uid for user in users]
        self.assertEqual(len(uids), len(set(uids))) 