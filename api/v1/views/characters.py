from rest_framework import viewsets, status, generics
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.shortcuts import get_object_or_404
import logging
from apps.characters.models import Character
from apps.characters.serializers import CharacterSerializer, CharacterDetailSerializer, CharacterDisplaySerializer

logger = logging.getLogger(__name__)

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
        character.save(regenerate_secret_key=True)
        return Response({
            'secret_key': character.secret_key
        })

    @action(detail=True, methods=['post'])
    def regenerate_display_code(self, request, pk=None):
        """重新生成角色的展示短码"""
        character = self.get_object()
        old_code = character.display_code
        character.display_code = character.generate_display_code()
        character.save()
        return Response({
            'old_code': old_code,
            'new_code': character.display_code
        })

class CharacterDisplayView(generics.RetrieveAPIView):
    """公开访问的角色展示视图"""
    queryset = Character.objects.filter(is_active=True)
    serializer_class = CharacterDisplaySerializer
    permission_classes = [AllowAny]
    lookup_field = 'display_code'
    lookup_url_kwarg = 'code'

    def get_object(self):
        """重写获取对象的方法，添加详细的错误处理"""
        queryset = Character.objects.all()  # 先获取所有角色
        code = self.kwargs.get(self.lookup_url_kwarg)
        
        logger.info(f"Attempting to find character with display_code: {code}")
        
        try:
            character = queryset.get(display_code=code)
            if not character.is_active:
                logger.info(f"Character {code} found but is inactive")
                raise Character.DoesNotExist("该角色已被禁用")
            logger.info(f"Character {code} found and is active")
            return character
        except Character.DoesNotExist:
            logger.info(f"No character found with display_code: {code}")
            raise Character.DoesNotExist("找不到该角色") 