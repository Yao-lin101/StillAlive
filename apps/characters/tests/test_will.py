from django.test import TestCase, override_settings, TransactionTestCase
from django.utils import timezone
from django.core import mail
from django.core.exceptions import ValidationError
from unittest.mock import patch
from datetime import timedelta, datetime
import pytz
import os
from django.conf import settings
from celery import current_app
from django.db import transaction

from apps.characters.models import Character, WillConfig, CharacterStatus
from apps.characters.tasks import check_wills, send_will_email
from apps.users.models import User

class WillConfigTest(TestCase):
    def setUp(self):
        # 创建测试用户
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # 创建测试角色
        self.character = Character.objects.create(
            user=self.user,
            name='Test Character',
            status_config={
                'display': {
                    'default_message': '在线',
                    'timeout_messages': [
                        {'hours': 24, 'message': '已离线一天'},
                        {'hours': 168, 'message': '已离线一周'}
                    ]
                }
            }
        )

    def test_will_config_creation(self):
        """测试遗嘱配置的创建"""
        will_config = WillConfig.objects.create(
            character=self.character,
            is_enabled=True,
            content='测试遗嘱内容',
            target_email='target@example.com',
            cc_emails=['cc1@example.com', 'cc2@example.com'],
            timeout_hours=24
        )

        self.assertEqual(will_config.character, self.character)
        self.assertTrue(will_config.is_enabled)
        self.assertEqual(will_config.content, '测试遗嘱内容')
        self.assertEqual(will_config.target_email, 'target@example.com')
        self.assertEqual(will_config.cc_emails, ['cc1@example.com', 'cc2@example.com'])
        self.assertEqual(will_config.timeout_hours, 24)

    def test_will_config_validation(self):
        """测试遗嘱配置的验证"""
        # 测试无效的邮箱格式
        with self.assertRaises(ValidationError):
            WillConfig.objects.create(
                character=self.character,
                is_enabled=True,
                content='测试内容',
                target_email='invalid-email',
                timeout_hours=24
            )

        # 测试无效的超时时间
        with self.assertRaises(ValidationError):
            WillConfig.objects.create(
                character=self.character,
                is_enabled=True,
                content='测试内容',
                target_email='valid@example.com',
                timeout_hours=0
            )

# 配置Celery为同步执行模式
@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,  # 任务同步执行
    CELERY_TASK_EAGER_PROPAGATES=True  # 同步执行时传播异常
)
class WillTasksTest(TransactionTestCase):
    def setUp(self):
        # 创建测试用户和角色
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        self.character = Character.objects.create(
            user=self.user,
            name='Test Character'
        )

        # 创建测试遗嘱配置
        self.will_config = WillConfig.objects.create(
            character=self.character,
            is_enabled=True,
            content='测试遗嘱内容',
            target_email='target@example.com',
            cc_emails=[],  # 提供空列表作为默认值
            timeout_hours=24
        )

    @patch('django.utils.timezone.now')
    def test_check_wills(self, mock_now):
        """测试遗嘱检查任务"""
        # 设置固定的测试时间
        base_time = timezone.make_aware(datetime(2024, 1, 1, 12, 0))
        past_time = base_time - timedelta(hours=25)
        
        # 设置模拟的当前时间
        mock_now.return_value = base_time
        print(f"\nTest time (now): {base_time}")

        # 创建过期的状态记录（25小时前）
        status = CharacterStatus.objects.create(
            character=self.character,
            data={},
            status_type='test'
        )
        # 直接更新时间戳
        CharacterStatus.objects.filter(id=status.id).update(timestamp=past_time)
        print(f"Created status at: {past_time}")

        # 确认初始状态
        self.assertTrue(self.will_config.is_enabled)
        print(f"Initial will_config.is_enabled: {self.will_config.is_enabled}")

        # 执行检查任务
        result = check_wills.apply()
        print(f"Task result: {result.get()}")

        # 从数据库重新获取配置对象
        self.will_config.refresh_from_db()
        print(f"Final will_config.is_enabled: {self.will_config.is_enabled}")
        
        # 验证遗嘱是否被触发（配置应该被禁用）
        self.assertFalse(self.will_config.is_enabled)
        
        # 验证邮件是否发送
        self.assertEqual(len(mail.outbox), 1)
        sent_mail = mail.outbox[0]
        self.assertEqual(sent_mail.to, [self.will_config.target_email])

    def test_send_will_email(self):
        """测试遗嘱邮件发送功能"""
        # 执行发送邮件任务
        result = send_will_email.apply(args=[self.will_config.id])
        print(f"\nEmail task result: {result.get()}")

        # 验证邮件是否发送
        self.assertEqual(len(mail.outbox), 1)
        sent_mail = mail.outbox[0]
        self.assertEqual(sent_mail.to, [self.will_config.target_email])
        self.assertIn(self.will_config.content, sent_mail.body)

    @patch('django.utils.timezone.now')
    def test_will_timeout_calculation(self, mock_now):
        """测试遗嘱超时计算和状态变更"""
        # 设置固定的测试时间
        base_time = timezone.make_aware(datetime(2024, 1, 1, 12, 0))
        expired_time = base_time - timedelta(hours=25)
        not_expired_time = base_time - timedelta(hours=23)
        
        # 设置模拟的当前时间
        mock_now.return_value = base_time
        print(f"\nTest time (now): {base_time}")

        # 先创建一个过期的状态记录
        status = CharacterStatus.objects.create(
            character=self.character,
            data={},
            status_type='test'
        )
        # 直接更新时间戳
        CharacterStatus.objects.filter(id=status.id).update(timestamp=expired_time)
        print(f"Created expired status at: {expired_time}")

        # 确认初始状态
        self.assertTrue(self.will_config.is_enabled)
        print(f"Initial will_config.is_enabled: {self.will_config.is_enabled}")

        # 执行检查任务
        result = check_wills.apply()
        print(f"Task result: {result.get()}")
        
        # 从数据库重新获取配置对象
        self.will_config.refresh_from_db()
        print(f"After first check will_config.is_enabled: {self.will_config.is_enabled}")
        
        # 验证遗嘱配置是否被禁用
        self.assertFalse(self.will_config.is_enabled)

        # 重新启用遗嘱配置
        self.will_config.is_enabled = True
        self.will_config.save()
        print(f"After re-enable will_config.is_enabled: {self.will_config.is_enabled}")

        # 创建一个未过期的状态记录
        status = CharacterStatus.objects.create(
            character=self.character,
            data={},
            status_type='test'
        )
        # 直接更新时间戳
        CharacterStatus.objects.filter(id=status.id).update(timestamp=not_expired_time)
        print(f"Created not expired status at: {not_expired_time}")

        # 执行检查任务
        result = check_wills.apply()
        print(f"Second task result: {result.get()}")
        
        # 从数据库重新获取配置对象
        self.will_config.refresh_from_db()
        print(f"Final will_config.is_enabled: {self.will_config.is_enabled}")
        
        # 验证遗嘱配置仍然启用
        self.assertTrue(self.will_config.is_enabled) 