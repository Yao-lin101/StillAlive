from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.validators import EmailValidator
from django.utils import timezone
from PIL import Image
import shortuuid
import os
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

def generate_uid():
    """生成10位纯数字的UID"""
    # 使用 shortuuid 生成纯数字的唯一标识符
    return 'smtx' + shortuuid.ShortUUID(alphabet="0123456789").uuid()[:10]

def avatar_upload_path(instance, filename):
    """生成头像文件路径，格式: avatars/uid_timestamp.ext"""
    ext = filename.split('.')[-1]
    timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
    return f'avatars/avatar_{instance.uid}_{timestamp}.{ext}'

class CustomUserManager(BaseUserManager):
    def create_user(self, password=None, **extra_fields):
        """
        创建普通用户
        """
        # 验证邮箱或微信至少有一个
        email = extra_fields.get('email')
        wechat_id = extra_fields.get('wechat_id')
        if not email and not wechat_id:
            raise ValueError('用户必须提供邮箱或微信账号')
            
        # 确保先生成 uid
        uid = extra_fields.get('uid') or generate_uid()
        extra_fields['uid'] = uid
        
        # 如果没有用户名，使用 uid
        if 'username' not in extra_fields:
            extra_fields['username'] = uid
            
        user = self.model(**extra_fields)
        if password:
            user.set_password(password)
        user.save()
        return user

    def create_superuser(self, username, password, **extra_fields):
        """
        创建超级用户
        """
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        
        return self.create_user(username=username, password=password, **extra_fields)

class User(AbstractUser):
    # 基本信息
    uid = models.CharField(max_length=14, unique=True, editable=False, 
                         default=generate_uid, verbose_name='UID')
    avatar = models.ImageField(upload_to=avatar_upload_path, null=True, blank=True, 
                             verbose_name='头像')
    bio = models.TextField(max_length=500, null=True, blank=True, verbose_name='个人简介')
    email = models.EmailField(unique=True, null=True, blank=True, 
                            validators=[EmailValidator()],
                            verbose_name='邮箱')
    wechat_id = models.CharField(max_length=100, unique=True, null=True, blank=True,
                               verbose_name='微信ID')
    
    # 验证状态
    is_email_verified = models.BooleanField(default=False, verbose_name='邮箱已验证')
    is_wechat_verified = models.BooleanField(default=False, verbose_name='微信已验证')
    
    # 时间信息
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    objects = CustomUserManager()

    class Meta:
        verbose_name = '用户'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.username}({self.uid})"

    def save(self, *args, **kwargs):
        if not self.pk and not self.username:
            self.username = self.uid
            
        # 处理头像更新
        if self.pk:
            try:
                old_instance = User.objects.get(pk=self.pk)
                # 如果头像发生变化且存在旧头像，则删除旧头像文件
                if old_instance.avatar and self.avatar != old_instance.avatar:
                    if os.path.isfile(old_instance.avatar.path):
                        os.remove(old_instance.avatar.path)
            except User.DoesNotExist:
                pass
            
        super().save(*args, **kwargs)
        
        # 处理新上传的头像
        if self.avatar:
            try:
                img = Image.open(self.avatar.path)
                # 统一调整为 300x300 像素
                output_size = (300, 300)
                img.thumbnail(output_size)
                # 如果不是正方形，进行居中裁剪
                if img.size[0] != img.size[1]:
                    width, height = img.size
                    new_size = min(width, height)
                    left = (width - new_size) // 2
                    top = (height - new_size) // 2
                    right = left + new_size
                    bottom = top + new_size
                    img = img.crop((left, top, right, bottom))
                img.save(self.avatar.path, quality=90, optimize=True)
            except Exception as e:
                print(f"处理头像时出错: {e}")

class BlacklistedUser(models.Model):
    # 谁拉黑的
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='blacklisted_users',
        to_field='uid',  # 使用 uid 作为外键
        db_column='user_uid'  # 数据库列名
    )
    # 被拉黑的用户
    blocked_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='blocked_by_users',
        to_field='uid',  # 使用 uid 作为外键
        db_column='blocked_user_uid'  # 数据库列名
    )
    reason = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['user', 'blocked_user']
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} blocked {self.blocked_user.username}"

def generate_invitation_code():
    """生成8位邀请码"""
    return shortuuid.ShortUUID(alphabet="23456789ABCDEFGHJKLMNPQRSTUVWXYZ").random(length=8)

class InvitationCode(models.Model):
    code = models.CharField(
        max_length=8,
        unique=True,
        default=generate_invitation_code,
        editable=False,
        verbose_name='邀请码'
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_invitation_codes',
        verbose_name='创建者'
    )
    used_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='used_invitation_code',
        verbose_name='使用者'
    )
    is_used = models.BooleanField(default=False, verbose_name='是否已使用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    used_at = models.DateTimeField(null=True, blank=True, verbose_name='使用时间')
    note = models.TextField(blank=True, null=True, verbose_name='备注')

    class Meta:
        verbose_name = '邀请码'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"邀请码: {self.code}"

    def use(self, user):
        """使用邀请码"""
        if self.is_used:
            raise ValueError('邀请码已被使用')
            
        self.is_used = True
        self.used_by = user
        self.used_at = timezone.now()
        self.save()

    @classmethod
    def create_invitation_code(cls, created_by, note=None):
        """
        创建新的邀请码
        :param created_by: 创建者（User对象）
        :param note: 备注信息
        :return: InvitationCode对象
        """
        return cls.objects.create(
            created_by=created_by,
            note=note
        )

    @property
    def is_valid(self):
        """检查邀请码是否有效"""
        return not self.is_used

