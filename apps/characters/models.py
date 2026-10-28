import uuid
import secrets
import string
from django.db import models
from django.utils import timezone
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.validators import MaxValueValidator, MinValueValidator
from pgvector.django import VectorField
from urllib.parse import urlsplit

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
    template_style = models.CharField(
        max_length=50,
        default='default',
        help_text='日报模板风格'
    )
    system_inferred_persona = models.TextField(
        blank=True,
        null=True,
        default='',
        help_text='系统暗中生成的真实人设档案（不对用户展示），用于修正LLM的长期认知'
    )
    persona_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='用户自述人设更新时间'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['character', 'is_enabled']),
        ]
        verbose_name = '日报配置'
        verbose_name_plural = '日报配置'

    def save(self, *args, **kwargs):
        if self.pk:
            try:
                old_instance = DailyReportConfig.objects.get(pk=self.pk)
                if old_instance.persona != self.persona:
                    self.persona_updated_at = timezone.now()
            except DailyReportConfig.DoesNotExist:
                if self.persona:
                    self.persona_updated_at = timezone.now()
        else:
            if self.persona:
                self.persona_updated_at = timezone.now()
        
        super().save(*args, **kwargs)

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


class ImportantEvent(models.Model):
    """
    长期重要事件记忆

    Postgres 是权威存储；向量库只保存 embedding 索引，可随时重建。
    """
    character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='important_events')
    source_report = models.ForeignKey(DailyReport, on_delete=models.CASCADE, related_name='important_events')
    date = models.DateField(help_text='事件所属日期')
    event_key = models.CharField(max_length=128, help_text='同一日报内稳定去重 key')
    title = models.CharField(max_length=120)
    summary = models.TextField()
    event_type = models.CharField(max_length=50, blank=True, default='')
    time_range = models.CharField(max_length=50, blank=True, default='')
    importance_score = models.PositiveSmallIntegerField(default=50, help_text='0-100，越高越重要')
    confidence = models.FloatField(default=0.8, help_text='0-1，抽取可信度')
    entities = models.JSONField(default=list, blank=True)
    keywords = models.JSONField(default=list, blank=True)
    evidence = models.JSONField(default=list, blank=True)
    source_hash = models.CharField(max_length=64, blank=True, default='')
    embedding_text = models.TextField(blank=True, default='')
    # pgvector 后端：embedding 与事件存在同一行；Milvus 后端则不使用此列。
    # 不固定维度：不同环境可用不同 embedding 模型（如 dev 768 / prod 1024），
    # 同一迁移即可通用。代价是无法建 HNSW 索引——个人数据量走精确搜索即可。
    embedding = VectorField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    # 通用「向量已建索引」标记（Milvus / pgvector 共用）。
    milvus_synced = models.BooleanField(default=False)
    milvus_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-importance_score']
        indexes = [
            models.Index(fields=['character', '-date']),
            models.Index(fields=['character', 'is_active', '-importance_score']),
            models.Index(fields=['source_report']),
            models.Index(fields=['event_type']),
        ]
        unique_together = ['character', 'date', 'event_key']
        verbose_name = '重要事件记忆'
        verbose_name_plural = '重要事件记忆'

    def __str__(self):
        return f"{self.character.name} - {self.date} - {self.title}"


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
        max_length=255,
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


class QQMessage(models.Model):
    """
    QQ消息记录
    用于存储QQ群消息和私聊消息数据
    """

    character = models.ForeignKey(
        Character,
        on_delete=models.CASCADE,
        related_name='qq_messages',
        help_text='关联的角色'
    )
    date = models.DateField(
        help_text='消息日期'
    )
    timestamp = models.DateTimeField(
        auto_now_add=True,
        help_text='记录创建时间'
    )
    message_data = models.JSONField(
        help_text='消息数据'
    )
    message_type = models.CharField(
        max_length=20,
        help_text='消息类型（group/private）'
    )

    class Meta:
        ordering = ['-date', '-timestamp']
        indexes = [
            models.Index(fields=['character', 'date']),
            models.Index(fields=['character', 'message_type']),
        ]
        verbose_name = 'QQ消息记录'
        verbose_name_plural = 'QQ消息记录'

    def __str__(self):
        if self.message_type == 'group':
            return f"[{self.date}] 群消息: {len(self.message_data)}个消息块"
        else:
            return f"[{self.date}] 私聊消息: {len(self.message_data)}个消息块"


class LLMProvider(models.Model):
    """供应商的协议、地址、凭据和请求策略；可被多个模型共享。"""

    API_ANTHROPIC = 'anthropic'
    API_GEMINI = 'gemini'
    API_OPENAI = 'openai'
    API_OPENAI_RESPONSES = 'openai_responses'
    API_CHOICES = [
        (API_ANTHROPIC, 'Anthropic'),
        (API_GEMINI, 'Gemini'),
        (API_OPENAI, 'OpenAI 兼容（Chat Completions）'),
        (API_OPENAI_RESPONSES, 'OpenAI Responses'),
    ]

    name = models.CharField('供应商名称', max_length=100, unique=True)
    api_type = models.CharField('接口类型', max_length=20, choices=API_CHOICES, default=API_ANTHROPIC)
    base_url = models.CharField(
        'Base URL', max_length=2048, blank=True, default='',
        help_text='留空使用官方地址。Anthropic / Gemini 填服务根地址；OpenAI 通常填到 /v1，'
                  '不要包含 /messages、/chat/completions 或 /responses。支持 HTTP/HTTPS 自建服务。',
    )
    api_key = models.CharField(
        'API Key', max_length=2048, blank=True, default='',
        help_text='密钥存储在数据库中，仅授权管理员可修改；无鉴权的 OpenAI 自建端点可留空。',
    )
    timeout = models.PositiveIntegerField('超时（秒）', default=180, validators=[MinValueValidator(1)])
    max_retries = models.PositiveSmallIntegerField(
        '单模型重试次数', default=3, validators=[MaxValueValidator(10)],
        help_text='当前模型重试耗尽后，按优先级降级到下一模型；0 表示不重试。',
    )
    is_active = models.BooleanField('启用', default=True, help_text='关闭后，此供应商的所有模型均不参与调用。')
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        ordering = ['name', 'pk']
        verbose_name = 'LLM 供应商'
        verbose_name_plural = 'LLM 供应商'

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self.name = (self.name or '').strip()
        self.api_key = (self.api_key or '').strip()
        self.base_url = (self.base_url or '').strip().rstrip('/')
        errors = {}
        if not self.name:
            errors['name'] = '请填写供应商名称。'
        if self.api_type not in dict(self.API_CHOICES):
            errors['api_type'] = '不支持的 LLM 接口类型。'
        if self.base_url:
            try:
                url = urlsplit(self.base_url)
                if (url.scheme not in ('http', 'https') or not url.hostname
                        or url.username or url.password or url.query or url.fragment):
                    raise ValueError
                url.port
            except ValueError:
                errors['base_url'] = '请填写有效的 HTTP/HTTPS Base URL，不能包含凭据、查询参数或片段。'
        key_optional = self.api_type in (self.API_OPENAI, self.API_OPENAI_RESPONSES) and self.base_url
        if self.is_active and not self.api_key and not key_optional:
            errors['api_key'] = '启用前请填写 API Key；无鉴权的 OpenAI 自建端点需要填写 Base URL。'
        if errors:
            raise ValidationError(errors)


class LLMModel(models.Model):
    """供应商下的模型目录；是否参与自动调用由独立调用链决定。"""

    provider = models.ForeignKey(LLMProvider, on_delete=models.CASCADE, related_name='models', verbose_name='供应商')
    model = models.CharField('模型 ID', max_length=255, help_text='请求发送给供应商的实际模型名称。')
    display_name = models.CharField('显示名称', max_length=100, blank=True, default='')
    max_tokens = models.PositiveIntegerField(
        '输出 Token 上限', default=8192, validators=[MinValueValidator(1)],
        help_text='此模型的输出上限；任务自身要求更小时使用较小值。',
    )
    temperature = models.FloatField(
        'Temperature', null=True, blank=True, default=None,
        validators=[MinValueValidator(0), MaxValueValidator(2)],
        help_text='留空沿用各任务的温度；填写后覆盖任务温度。Anthropic 范围为 0–1，其余为 0–2。',
    )
    is_active = models.BooleanField('启用', default=True, help_text='停用后调用链会跳过此模型；启用不会自动加入调用链。')
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        ordering = ['provider__name', 'model', 'pk']
        verbose_name = 'LLM 模型'
        verbose_name_plural = 'LLM 模型'
        constraints = [
            models.UniqueConstraint(fields=['provider', 'model'], name='llm_model_unique_per_provider'),
        ]

    def __str__(self):
        return f'{self.provider}: {self.display_name or self.model}'

    def clean(self):
        super().clean()
        self.model = (self.model or '').strip()
        if not self.model:
            raise ValidationError({'model': '请填写模型 ID。'})
        try:
            provider = self.provider
        except LLMProvider.DoesNotExist:
            return
        if provider.api_type == LLMProvider.API_ANTHROPIC and self.temperature is not None and self.temperature > 1:
            raise ValidationError({'temperature': 'Anthropic 的 Temperature 不能超过 1。'})


class LLMRoutingConfig(models.Model):
    """独立的全局调用链；模型目录中的记录不会自动参与调用。"""

    is_active = models.BooleanField('启用调用链', default=True, help_text='关闭后暂停所有自动 LLM 调用。')
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = 'LLM 调用链配置'
        verbose_name_plural = 'LLM 调用链配置'
        constraints = [
            models.CheckConstraint(condition=models.Q(pk=1), name='llm_routing_config_singleton'),
        ]

    def __str__(self):
        return '默认 LLM 调用链'

    def clean(self):
        super().clean()
        self.pk = 1

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class LLMRoutingEntry(models.Model):
    config = models.ForeignKey(
        LLMRoutingConfig, on_delete=models.CASCADE, related_name='entries', verbose_name='调用链',
    )
    model = models.ForeignKey(
        LLMModel, on_delete=models.PROTECT, related_name='routing_entries', verbose_name='模型',
        help_text='从所有供应商已配置的模型中选择；未加入调用链的模型不会被自动调用。',
    )
    priority = models.PositiveIntegerField(
        '调用顺序', default=10,
        help_text='数字越小越先调用；首个可用模型为首选，失败后依次使用后续模型。相同数值按记录 ID 排序。',
    )

    class Meta:
        ordering = ['priority', 'pk']
        verbose_name = '调用链模型'
        verbose_name_plural = '调用链模型（按调用顺序从小到大执行）'
        constraints = [
            models.UniqueConstraint(fields=['config', 'model'], name='llm_routing_model_unique'),
        ]

    def __str__(self):
        return f'{self.priority}: {self.model}'


class EmbeddingConfig(models.Model):
    """
    全局嵌入模型配置（单例，pk 固定为 1）。

    取代原先散落在环境变量里的 OLLAMA_* 设置：运行时以本表为准，
    首次访问时自动用现有 settings 播种，保证老部署平滑过渡。
    """
    PROVIDER_OLLAMA = 'ollama'
    PROVIDER_OPENAI = 'openai'
    PROVIDER_CHOICES = [
        (PROVIDER_OLLAMA, 'Ollama'),
        (PROVIDER_OPENAI, 'OpenAI 兼容'),
    ]

    provider = models.CharField(
        max_length=20, choices=PROVIDER_CHOICES, default=PROVIDER_OLLAMA,
        help_text='嵌入服务类型',
    )
    base_url = models.CharField(
        max_length=255, default='http://127.0.0.1:11434',
        help_text='Ollama 形如 http://127.0.0.1:11434；OpenAI 兼容形如 https://api.openai.com/v1',
    )
    api_key = models.CharField(
        max_length=255, blank=True, default='',
        help_text='OpenAI 兼容端点的密钥；Ollama 可留空。明文存储，请仅在受信任的自托管环境使用。',
    )
    model = models.CharField(
        max_length=100, default='nomic-embed-text',
        help_text='嵌入模型名，如 nomic-embed-text / mxbai-embed-large / text-embedding-3-small',
    )
    dimensions = models.PositiveIntegerField(
        default=768,
        help_text='向量维度。用于 Milvus 建表与 OpenAI dimensions 参数；pgvector 列不固定维度。'
                  '⚠️ 改动维度/模型后必须点「重建向量」，否则旧向量与新查询维度不一致会导致检索失效。',
    )
    max_chars = models.PositiveIntegerField(
        default=900, help_text='单条文本送入嵌入前的截断上限',
    )
    timeout = models.PositiveIntegerField(
        default=60, help_text='单次嵌入请求超时（秒）',
    )
    is_active = models.BooleanField(default=True, help_text='关闭则停用语义检索，退回元数据兜底')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '嵌入模型配置'
        verbose_name_plural = '嵌入模型配置'

    def __str__(self):
        return f"EmbeddingConfig({self.provider}:{self.model})"

    def save(self, *args, **kwargs):
        self.pk = 1  # 强制单例
        super().save(*args, **kwargs)

    @classmethod
    def _defaults_from_settings(cls):
        from django.conf import settings
        return {
            'provider': cls.PROVIDER_OLLAMA,
            'base_url': getattr(settings, 'OLLAMA_BASE_URL', 'http://127.0.0.1:11434'),
            'model': getattr(settings, 'OLLAMA_EMBED_MODEL', 'nomic-embed-text'),
            'dimensions': int(getattr(settings, 'OLLAMA_EMBED_DIM', 768)),
            'max_chars': int(getattr(settings, 'OLLAMA_EMBED_MAX_CHARS', 900)),
        }

    @classmethod
    def load(cls):
        """返回单例配置；不存在时用当前 settings 播种创建。"""
        obj, _ = cls.objects.get_or_create(pk=1, defaults=cls._defaults_from_settings())
        return obj
