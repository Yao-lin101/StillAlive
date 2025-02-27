import uuid
import secrets
import string
from django.db import models
from django.conf import settings
from django.utils import timezone

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
    avatar = models.ImageField(
        upload_to=character_avatar_path,
        null=True,
        blank=True
    )
    bio = models.TextField(max_length=500, blank=True)
    secret_key = models.UUIDField(default=uuid.uuid4, unique=True)
    display_code = models.CharField(max_length=6, unique=True, db_index=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    # 添加状态配置字段
    status_config = models.JSONField(default=dict, blank=True, help_text="""
    状态配置示例：
    {
        "vital_signs": {
            "ele": {
                "label": "当前电量",
                "suffix": "%",
                "color": {
                    "type": "threshold",
                    "rules": [
                        {"value": 20, "color": "red"},
                        {"value": 50, "color": "yellow"},
                        {"default": "green"}
                    ]
                }
            }
        },
        "display": {
            "default_message": "状态良好",
            "timeout_messages": [
                {"hours": 1, "message": "可能在摸鱼"},
                {"hours": 24, "message": "已经失联一天了"},
                {"hours": 168, "message": "已经一周没有消息了"}
            ]
        },
        "theme": {
            "background_url": "https://example.com/bg.jpg"
        }
    }
    """)

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

