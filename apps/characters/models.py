import uuid
from django.db import models
from django.conf import settings
from django.core.validators import URLValidator

def character_avatar_path(instance, filename):
    # 文件将被上传到 MEDIA_ROOT/avatars/user_<uid>/character_<uid>/<filename>
    return f'avatars/user_{instance.user.uid}/character_{instance.uid}/{filename}'

class Character(models.Model):
    uid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='characters'
    )
    name = models.CharField(max_length=50)
    avatar = models.ImageField(
        upload_to=character_avatar_path,
        null=True,
        blank=True
    )
    bio = models.TextField(max_length=500, blank=True)
    secret_key = models.UUIDField(default=uuid.uuid4, unique=True)
    display_url = models.URLField(
        max_length=200,
        blank=True,
        validators=[URLValidator()]
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['secret_key']),
        ]

    def __str__(self):
        return f"{self.name} ({self.uid})"

    def save(self, *args, **kwargs):
        # 如果是新创建的角色或强制重新生成secret_key
        if not self.pk or kwargs.pop('regenerate_secret_key', False):
            self.secret_key = uuid.uuid4()
        super().save(*args, **kwargs)
