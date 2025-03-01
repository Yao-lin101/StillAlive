from rest_framework import serializers
from django.conf import settings
from .models import Character, CharacterStatus
import logging

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
            'status_config'
        ]
        read_only_fields = ['uid', 'display_code', 'created_at', 'updated_at']

    def validate(self, attrs):
        """验证用户创建的角色数量"""
        user = self.context['request'].user
        if not user.is_superuser and self.instance is None:  # 只在创建新角色时验证
            character_count = Character.objects.filter(user=user).count()
            if character_count >= 3:
                raise serializers.ValidationError("非超级用户最多只能创建2个角色")
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
            
            return value
        except Exception as e:
            raise serializers.ValidationError(str(e))

class CharacterDisplaySerializer(serializers.ModelSerializer):
    """用于公开展示的角色序列化器"""
    class Meta:
        model = Character
        fields = ['name', 'avatar', 'bio', 'status_config']
        read_only_fields = fields 

class CharacterStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = CharacterStatus
        fields = ['status_type', 'data', 'timestamp']
        read_only_fields = ['timestamp']

class CharacterStatusUpdateSerializer(serializers.Serializer):
    type = serializers.CharField(max_length=50)
    data = serializers.JSONField()

class CharacterStatusResponseSerializer(serializers.Serializer):
    status = serializers.CharField()  # online/offline
    last_updated = serializers.DateTimeField(allow_null=True)
    status_data = serializers.DictField(
        child=serializers.DictField(
            child=serializers.JSONField()
        )
    ) 