from rest_framework import serializers
from django.conf import settings
from .models import Character, CharacterStatus, WillConfig, Message, DailyReportConfig, DailyReport
import logging
import json

logger = logging.getLogger(__name__)

class CharacterSerializer(serializers.ModelSerializer):
    status_config = serializers.JSONField(required=False, allow_null=True)
    
    class Meta:
        model = Character
        fields = [
            'uid', 
            'name', 
            'avatar', 
            'bio', 
            'display_code',
            'created_at', 
            'updated_at', 
            'is_active',
            'is_public',
            'status_config',
            'experience'
        ]
        read_only_fields = ['uid', 'display_code', 'created_at', 'updated_at', 'experience']

    def validate(self, attrs):
        """验证用户创建的角色数量"""
        user = self.context['request'].user
        if not user.is_superuser and self.instance is None:  # 只在创建新角色时验证
            character_count = Character.objects.filter(user=user).count()
            if character_count >= 4:
                raise serializers.ValidationError("算你厉害，但是也别太贪心了")
        return attrs

    def create(self, validated_data):
        # 确保创建时 is_active 为 True
        validated_data['is_active'] = True
        # 确保当前用户被设置为角色的所有者
        validated_data['user'] = self.context['request'].user
        # 生成 display_code
        instance = super().create(validated_data)
        if not instance.display_code:
            instance.display_code = instance.generate_display_code()
            instance.save()
        return instance

class CharacterDetailSerializer(CharacterSerializer):
    secret_key = serializers.UUIDField(read_only=True)
    
    class Meta(CharacterSerializer.Meta):
        fields = CharacterSerializer.Meta.fields + ['secret_key']

    def validate_status_config(self, value):
        """验证状态配置数据"""
        try:
            if not isinstance(value, dict):
                raise serializers.ValidationError("状态配置必须是一个对象")
            
            if 'vital_signs' not in value:
                raise serializers.ValidationError("状态配置必须包含 vital_signs 字段")
            
            vital_signs = value['vital_signs']
            if not isinstance(vital_signs, dict):
                raise serializers.ValidationError("vital_signs 必须是一个对象")
            
            # 验证配置长度
            config_str = json.dumps(value)
            if len(config_str) > 10000:
                raise serializers.ValidationError("状态配置长度不能超过10000个字符")
            
            return value
        except Exception as e:
            raise serializers.ValidationError(str(e))

class CharacterDisplaySerializer(serializers.ModelSerializer):
    """用于公开展示的角色序列化器"""
    is_owner = serializers.SerializerMethodField()
    uid = serializers.SerializerMethodField()

    class Meta:
        model = Character
        fields = ['uid', 'name', 'avatar', 'bio', 'status_config', 'is_owner', 'experience']
        read_only_fields = fields 

    def get_is_owner(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.user == request.user
        return False 

    def get_uid(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated and obj.user == request.user:
            return str(obj.uid)
        return None 

class CharacterStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = CharacterStatus
        fields = ['status_type', 'data', 'timestamp']
        read_only_fields = ['timestamp']

class CharacterStatusUpdateSerializer(serializers.Serializer):
    type = serializers.CharField(max_length=50)
    data = serializers.JSONField()

class CharacterStatusItemSerializer(serializers.Serializer):
    data = serializers.JSONField()
    updated_at = serializers.DateTimeField()

class CharacterStatusResponseSerializer(serializers.Serializer):
    status = serializers.CharField()  # online/offline
    last_updated = serializers.DateTimeField(allow_null=True)
    status_data = serializers.DictField(
        child=CharacterStatusItemSerializer()
    )

class WillConfigSerializer(serializers.ModelSerializer):
    cc_emails = serializers.ListField(
        child=serializers.EmailField(),
        required=False,
        default=list
    )
    content = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = WillConfig
        fields = [
            'is_enabled',
            'content',
            'target_email',
            'cc_emails',
            'timeout_hours',
            'created_at'
        ]
        read_only_fields = ['created_at']

    def validate(self, data):
        """验证数据，只有在启用遗嘱时才检查必填字段"""
        # 获取is_enabled字段的值，如果不存在则使用默认值False
        is_enabled = data.get('is_enabled')
        
        # 只有在明确启用遗嘱时才验证必填字段
        if is_enabled is True:  # 明确检查是否为True
            # 验证目标邮箱
            if not data.get('target_email'):
                # 检查是否是部分更新，以及实例是否已经有target_email
                instance = getattr(self, 'instance', None)
                if instance and instance.target_email:
                    # 如果是更新现有实例，且实例已有target_email，则不需要再提供
                    pass
                else:
                    # 如果是新建或实例没有target_email，则必须提供
                    raise serializers.ValidationError({'target_email': '启用遗嘱时必须提供目标邮箱'})
        
        return data

    def validate_cc_emails(self, value):
        if len(value) > 5:  # 限制抄送邮箱数量
            raise serializers.ValidationError("抄送邮箱不能超过5个")
        return value

    def validate_timeout_hours(self, value):
        if value < 24:  # 最短24小时
            raise serializers.ValidationError("触发时间不能少于24小时")
        if value > 8760:  # 最长一年
            raise serializers.ValidationError("触发时间不能超过一年")
        return value

class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ['id', 'content', 'created_at', 'ip_address', 'location']
        read_only_fields = ['id', 'created_at', 'ip_address', 'location']


class DailyReportConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyReportConfig
        fields = [
            'is_enabled',
            'visibility',
            'field_mappings',
            'persona',
            'ai_persona',
            'template_style',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate_field_mappings(self, value):
        """验证字段映射格式"""
        if not isinstance(value, dict):
            raise serializers.ValidationError("字段映射必须是一个对象")
        
        allowed_keys = {'phone_app', 'computer_app', 'steps'}
        for key in value.keys():
            if key not in allowed_keys:
                raise serializers.ValidationError(f"不支持的字段类型: {key}，支持的类型: phone_app, computer_app, steps")
        
        return value

    def validate_persona(self, value):
        """验证角色人设长度"""
        if value and len(value) > 1000:
            raise serializers.ValidationError(f"角色人设不能超过 1000 个字符（当前: {len(value)} 个字符）")
        return value

    def validate_ai_persona(self, value):
        """验证 AI 人设配置格式和长度"""
        if value is None:
            return {}
        
        if not isinstance(value, dict):
            raise serializers.ValidationError("AI 人设配置必须是一个对象")
        
        allowed_keys = {'core_identity', 'personality_traits', 'language_style'}
        for key in value.keys():
            if key not in allowed_keys:
                raise serializers.ValidationError(f"不支持的 AI 人设字段: {key}，支持的字段: core_identity, personality_traits, language_style")
        
        if 'core_identity' in value and value['core_identity'] and len(value['core_identity']) > 300:
            raise serializers.ValidationError(f"核心身份不能超过 300 个字符（当前: {len(value['core_identity'])} 个字符）")
        
        if 'personality_traits' in value and value['personality_traits'] and len(value['personality_traits']) > 500:
            raise serializers.ValidationError(f"性格特征不能超过 500 个字符（当前: {len(value['personality_traits'])} 个字符）")
        
        if 'language_style' in value and value['language_style'] and len(value['language_style']) > 500:
            raise serializers.ValidationError(f"语言风格不能超过 500 个字符（当前: {len(value['language_style'])} 个字符）")
        
        return value


class DailyReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyReport
        fields = [
            'id',
            'date',
            'is_hidden',
            'raw_data',
            'analysis_result',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'date', 'raw_data', 'analysis_result', 'created_at', 'updated_at']


class DailyReportDetailSerializer(serializers.ModelSerializer):
    markdown = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()
    report_data = serializers.SerializerMethodField()

    class Meta:
        model = DailyReport
        fields = [
            'date',
            'is_hidden',
            'markdown',
            'error',
            'report_data',
        ]

    def get_markdown(self, obj):
        if obj.analysis_result and isinstance(obj.analysis_result, dict):
            return obj.analysis_result.get('markdown', '')
        return ''

    def get_error(self, obj):
        if obj.analysis_result and isinstance(obj.analysis_result, dict):
            return obj.analysis_result.get('error')
        return None

    def get_report_data(self, obj):
        """生成结构化HTML报告数据（图表数据 + LLM评论）"""
        analysis_result = obj.analysis_result or {}
        if analysis_result.get('version', 1) < 2:
            return None

        try:
            from .services.html_report_service import build_report_data
            raw_data = obj.raw_data or {}
            return build_report_data(raw_data, analysis_result)
        except Exception as e:
            logger.error(f"Failed to build report_data for report {obj.id}: {e}")
            return {}