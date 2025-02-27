from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from apps.characters.models import Character
from apps.characters.serializers import CharacterSerializer, CharacterDetailSerializer

class CharacterViewSet(viewsets.ModelViewSet):
    """
    角色管理 API v1
    
    提供角色的创建、查询、更新、删除等功能
    """
    permission_classes = [IsAuthenticated]
    serializer_class = CharacterSerializer
    
    def get_queryset(self):
        """只返回当前用户的角色"""
        return Character.objects.filter(user=self.request.user)
    
    def get_serializer_class(self):
        """根据操作类型返回不同的序列化器"""
        if self.action in ['retrieve', 'create']:
            return CharacterDetailSerializer
        return CharacterSerializer
    
    @action(detail=True, methods=['get'])
    def secret_key(self, request, pk=None):
        """获取角色的secret_key"""
        character = self.get_object()
        return Response({
            'secret_key': character.secret_key
        })
    
    @action(detail=True, methods=['post'])
    def regenerate_secret_key(self, request, pk=None):
        """重新生成角色的secret_key"""
        character = self.get_object()
        character.save()  # 触发save方法中的secret_key重新生成
        return Response({
            'secret_key': character.secret_key
        }) 