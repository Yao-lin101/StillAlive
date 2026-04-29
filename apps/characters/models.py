import uuid
import secrets
import string
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

def get_default_status_config():
    return {
        "theme": {
            "background_url": "https://infinitypro-img.infinitynewtab.com/wallpaper/anime/408.jpg?imageView2/2/w/3200/format/webp/interlace/1"
        },
        "display": {
            "default_message": "还活着！",
            "timeout_messages": [
                {"hours": 3, "message": "有一会没碰手机了"},
                {"hours": 6, "message": "可能睡着了"},
                {"hours": 12, "message": "应该还活着..."},
                {"hours": 24, "message": "怕不是似了"}
            ]
        },
        "vital_signs": {
            "status_1": {
                "key": "battery",
                "label": "电量",
                "suffix": "%",
                "valueType": "number",
                "description": "设备电量"
            },
            "status_2": {
                "key": "phone",
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
    is_public = models.BooleanField(default=False, help_text='是否在存活者列表公开显示')
    
    # 修改状态配置字段的默认值为可调用对象
    status_config = models.JSONField(default=get_default_status_config, blank=True)
    
    # 经验值系统
    experience = models.PositiveIntegerField(default=0, help_text='角色总经验值')
    sync_streak = models.PositiveIntegerField(default=0, help_text='连续同步天数')
    last_sync_date = models.DateField(null=True, blank=True, help_text='上次同步日期')
    danmaku_ips_today = models.JSONField(default=list, blank=True, help_text='今日已贡献经验的IP列表')
    danmaku_ips_date = models.DateField(null=True, blank=True, help_text='IP列表对应的日期')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['secret_key']),
            models.Index(fields=['display_code']),
            models.Index(fields=['is_public']),
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
    content = models.TextField(help_text='遗嘱内容', blank=True, null=True, default='')
    target_email = models.EmailField(help_text='主要收件人邮箱')
    cc_emails = models.JSONField(default=list, blank=True, help_text='抄送邮箱列表')
    timeout_hours = models.IntegerField(default=24, help_text='触发时间（小时）')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['character', 'is_enabled']),
        ]
        verbose_name = '遗嘱配置'
        verbose_name_plural = '遗嘱配置'

    def clean(self):
        if self.timeout_hours <= 0:
            raise ValidationError({'timeout_hours': '超时时间必须大于0'})

        # 验证主要收件人邮箱
        try:
            validate_email(self.target_email)
        except ValidationError:
            raise ValidationError({'target_email': '无效的邮箱格式'})

        # 验证抄送邮箱列表
        if self.cc_emails:
            for email in self.cc_emails:
                try:
                    validate_email(email)
                except ValidationError:
                    raise ValidationError({'cc_emails': f'无效的抄送邮箱格式: {email}'})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.character.name}的遗嘱配置"


class Message(models.Model):
    character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='messages')
    content = models.TextField(max_length=500, help_text='留言内容')
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name="IP地址")
    location = models.CharField(max_length=100, blank=True, null=True, verbose_name="地理位置")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['character', '-created_at']),
        ]
        verbose_name = '留言'
        verbose_name_plural = '留言'


class DailyReportConfig(models.Model):
    VISIBILITY_CHOICES = [
        ('private', '仅自己可见'),
        ('public', '所有人可见'),
    ]

    character = models.OneToOneField(Character, on_delete=models.CASCADE, related_name='daily_report_config')
    is_enabled = models.BooleanField(default=False, help_text='是否启用日报分析')
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default='private',
        help_text='日报可见范围'
    )
    field_mappings = models.JSONField(
        default=dict,
        blank=True,
        help_text='字段映射关系，格式: {"phone_app": "状态key", "computer_app": "状态key", "steps": "状态key"}'
    )
    persona = models.TextField(
        blank=True,
        null=True,
        default='',
        help_text='角色人设信息，用于LLM分析日报时作为背景参考，如：年龄、职业、日常习惯等'
    )
    ai_persona = models.JSONField(
        default=dict,
        blank=True,
        help_text='AI 人设配置，用于自定义日报分析时的 AI 身份，格式: {"core_identity": "核心身份", "personality_traits": "性格特征", "language_style": "语言风格"}'
    )
    system_inferred_persona = models.TextField(
        blank=True,
        null=True,
        default='',
        help_text='系统暗中生成的真实人设档案（不对用户展示），用于修正LLM的长期认知'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['character', 'is_enabled']),
        ]
        verbose_name = '日报配置'
        verbose_name_plural = '日报配置'

    def __str__(self):
        return f"{self.character.name}的日报配置"


class DailyReport(models.Model):
    character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='daily_reports')
    date = models.DateField(help_text='日报日期')
    is_hidden = models.BooleanField(default=False, help_text='是否隐藏（隐藏后仅自己可见）')
    raw_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='原始聚合数据'
    )
    analysis_result = models.JSONField(
        default=dict,
        blank=True,
        help_text='AI分析结果'
    )
    last_record_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text='最新状态数据的时间，用于判断是否有新数据'
    )
    data_cutoff_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text='数据截止时间（任务执行时的时间）'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['character', '-date']),
            models.Index(fields=['character', 'date']),
            models.Index(fields=['date']),
        ]
        unique_together = ['character', 'date']
        verbose_name = '日报'
        verbose_name_plural = '日报'

    def __str__(self):
        return f"{self.character.name} - {self.date}"


class PersonaHistory(models.Model):
    """
    侧写历史记录
    用于追踪系统推断人设（system_inferred_persona）的变化过程
    不影响当前侧写，仅用于历史追溯和分析
    
    设计原则：
    1. 每个记录只存"当前这次生成的侧写"
    2. 每天只能有一条记录（如同一天更新多次则覆盖）
    3. 通过时间顺序自然形成历史链
    """
    TRIGGER_SCHEDULED = 'scheduled'
    TRIGGER_MANUAL = 'manual'
    TRIGGER_CHOICES = [
        (TRIGGER_SCHEDULED, '定时任务'),
        (TRIGGER_MANUAL, '手动触发'),
    ]

    config = models.ForeignKey(
        DailyReportConfig,
        on_delete=models.CASCADE,
        related_name='persona_history',
        help_text='关联的日报配置'
    )
    date = models.DateField(
        help_text='记录日期（每天一条）'
    )
    generated_at = models.DateTimeField(
        auto_now=True,
        help_text='最后更新时间'
    )
    persona_content = models.TextField(
        blank=True,
        null=True,
        default='',
        help_text='本次生成的侧写内容'
    )
    data_dates = models.JSONField(
        default=list,
        blank=True,
        help_text='本次侧写基于哪些日期的数据（日期列表）'
    )
    is_first_time = models.BooleanField(
        default=False,
        help_text='是否是首次生成侧写'
    )
    model_used = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text='使用的 LLM 模型名称'
    )
    trigger_type = models.CharField(
        max_length=20,
        choices=TRIGGER_CHOICES,
        default=TRIGGER_SCHEDULED,
        help_text='触发方式'
    )

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['config', '-date']),
            models.Index(fields=['date']),
        ]
        unique_together = ['config', 'date']  # 每个 config 每天只能有一条记录
        verbose_name = '侧写历史记录'
        verbose_name_plural = '侧写历史记录'

    def __str__(self):
        return f"{self.config.character.name} - {self.date}"

    @property
    def previous_record(self):
        """获取上一条历史记录（用于对比变化）"""
        return PersonaHistory.objects.filter(
            config=self.config,
            date__lt=self.date
        ).order_by('-date').first()

    def has_changed_from_previous(self):
        """判断与上一条记录相比是否发生了变化"""
        prev = self.previous_record
        if not prev:
            return True  # 首次记录，视为变化
        prev_content = (prev.persona_content or '').strip()
        curr_content = (self.persona_content or '').strip()
        return prev_content != curr_content

