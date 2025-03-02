import uuid
import secrets
import string
from django.db import models
from django.conf import settings
from django.utils import timezone

def get_default_status_config():
    return {
        "theme": {
            "accent_color": "from-blue-400 to-purple-400",
            "background_url": "https://infinitypro-img.infinitynewtab.com/wallpaper/anime/408.jpg?imageView2/2/w/3200/format/webp/interlace/1",
            "background_overlay": "from-gray-900/95 to-gray-800/95"
        },
        "display": {
            "default_message": "还活着！",
            "timeout_messages": [
                {"hours": 24, "message": "怕不是似了"},
                {"hours": 12, "message": "应该还活着..."},
                {"hours": 6, "message": "可能睡着了"},
                {"hours": 3, "message": "有一会没碰手机了"}
            ]
        },
        "vital_signs": {
            "status_1": {
                "key": "battery",
                "color": {
                    "type": "threshold",
                    "rules": [
                        {"color": "#ff0000", "value": 0},
                        {"color": "#00ff00", "value": 100}
                    ]
                },
                "label": "电量",
                "suffix": "%",
                "valueType": "number",
                "description": "设备电量"
            },
            "status_2": {
                "key": "phone",
                "color": {
                    "type": "threshold",
                    "rules": [
                        {"color": "#ff0000", "value": 0},
                        {"color": "#00ff00", "value": 100}
                    ]
                },
                "label": "正在使用",
                "valueType": "text",
                "description": "显示正在使用的APP"
            },
            "status_3": {
                "key": "location",
                "label": "位置",
                "valueType": "text",
                "description": "所在城市"
            },
            "status_4": {
                "key": "weather",
                "label": "天气",
                "valueType": "text",
                "description": "城市天气"
            }
        }
    }

def character_avatar_path(instance, filename):
    # 文件将被上传到 MEDIA_ROOT/avatars/user_<uid>/character_<uid>/<filename>
    return f'avatars/user_{instance.user.uid}/character_{instance.uid}/{filename}'

class Character(models.Model):
    uid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='characters',
        to_field='uid',
        db_column='user_uid'
    )
    name = models.CharField(max_length=50)
    avatar = models.URLField(
        max_length=500,
        null=True,
        blank=True,
        help_text='角色头像URL地址'
    )
    bio = models.TextField(max_length=500, blank=True)
    secret_key = models.UUIDField(default=uuid.uuid4, unique=True)
    display_code = models.CharField(max_length=6, unique=True, db_index=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    # 修改状态配置字段的默认值为可调用对象
    status_config = models.JSONField(default=get_default_status_config, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['secret_key']),
            models.Index(fields=['display_code']),
        ]

    def __str__(self):
        return f"{self.name} ({self.uid})"

    def generate_display_code(self):
        chars = string.ascii_letters + string.digits
        while True:
            code = ''.join(secrets.choice(chars) for _ in range(6))
            if not Character.objects.filter(display_code=code).exists():
                return code

    def save(self, *args, **kwargs):
        if not self.pk and not self.display_code:
            self.display_code = self.generate_display_code()
        # 如果是新创建的角色或强制重新生成secret_key
        if not self.pk or kwargs.pop('regenerate_secret_key', False):
            self.secret_key = uuid.uuid4()
        super().save(*args, **kwargs)

class CharacterStatus(models.Model):
    character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='statuses')
    timestamp = models.DateTimeField(auto_now_add=True)
    status_type = models.CharField(max_length=50)
    data = models.JSONField()
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['character', 'status_type', 'timestamp']),
        ]
        verbose_name = '角色状态'
        verbose_name_plural = '角色状态'
        
    @classmethod
    def get_latest_status(cls, character):
        """获取角色所有类型的最新状态"""
        latest_by_type = cls.objects.filter(
            character=character
        ).values('status_type').annotate(
            latest_id=models.Max('id')
        )
        
        return cls.objects.filter(
            id__in=[item['latest_id'] for item in latest_by_type]
        )

class WillConfig(models.Model):
    character = models.OneToOneField(Character, on_delete=models.CASCADE, related_name='will_config')
    is_enabled = models.BooleanField(default=False)
    content = models.TextField(help_text='遗嘱内容')
    target_email = models.EmailField(help_text='主要收件人邮箱')
    cc_emails = models.JSONField(default=list, help_text='抄送邮箱列表')
    timeout_hours = models.IntegerField(default=168, help_text='触发时间（小时）')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['character', 'is_enabled']),
        ]
        verbose_name = '遗嘱配置'
        verbose_name_plural = '遗嘱配置'

    def __str__(self):
        return f"{self.character.name}的遗嘱配置"

