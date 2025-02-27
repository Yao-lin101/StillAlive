from rest_framework import serializers
from django.conf import settings
from .models import Character, CharacterStatus

class CharacterSerializer(serializers.ModelSerializer):
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