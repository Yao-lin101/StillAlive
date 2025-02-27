from rest_framework import serializers
from django.conf import settings
from .models import Character

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
            'is_active'
        ]
        read_only_fields = ['uid', 'display_code', 'created_at', 'updated_at']

    def create(self, validated_data):
        # 确保当前用户被设置为角色的所有者
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)

class CharacterDetailSerializer(CharacterSerializer):
    secret_key = serializers.UUIDField(read_only=True)
    
    class Meta(CharacterSerializer.Meta):
        fields = CharacterSerializer.Meta.fields + ['secret_key']

class CharacterDisplaySerializer(serializers.ModelSerializer):
    """用于公开展示的角色序列化器"""
    class Meta:
        model = Character
        fields = ['name', 'avatar', 'bio']
        read_only_fields = fields 