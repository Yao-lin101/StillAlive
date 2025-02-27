from rest_framework import viewsets, status, generics
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.shortcuts import get_object_or_404
from django.utils import timezone
from datetime import timedelta
import logging

from apps.characters.models import Character, CharacterStatus
from apps.characters.serializers import (
    CharacterSerializer, CharacterDetailSerializer, CharacterDisplaySerializer,
    CharacterStatusUpdateSerializer, CharacterStatusResponseSerializer
)

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

    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        """更新角色的激活状态"""
        character = self.get_object()
        is_active = request.data.get('is_active')
        if is_active is None:
            return Response(
                {'error': '缺少 is_active 参数'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        character.is_active = is_active
        character.save()
        return Response({
            'is_active': character.is_active
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

@api_view(['POST'])
@permission_classes([AllowAny])
def update_character_status(request):
    """通过快捷指令更新角色状态"""
    try:
        # 从请求头获取秘钥
        secret_key = request.headers.get('X-Character-Key')
        if not secret_key:
            return Response(
                {'error': '缺少认证秘钥'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # 验证请求数据
        serializer = CharacterStatusUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        # 验证 secret_key
        character = get_object_or_404(Character, secret_key=secret_key)
        
        # 创建新状态记录
        CharacterStatus.objects.create(
            character=character,
            status_type=serializer.validated_data['type'],
            data=serializer.validated_data['data']
        )

        return Response({'status': 'success'})
    except Exception as e:
        logger.error(f"更新状态失败: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )

@api_view(['GET'])
@permission_classes([AllowAny])
def get_character_status(request, code):
    """获取角色的最新状态"""
    try:
        character = get_object_or_404(Character, display_code=code)
        
        # 获取所有类型的最新状态
        latest_statuses = CharacterStatus.get_latest_status(character)
        
        # 将状态数据按类型组织
        status_data = {}
        last_updated = None
        
        for status in latest_statuses:
            status_data[status.status_type] = {
                'data': status.data,
                'updated_at': status.timestamp
            }
            # 使用最新的高频数据时间作为在线状态判断
            if status.status_type == 'vital_signs':
                last_updated = status.timestamp

        # 如果最后更新时间在15分钟内，认为是在线状态
        is_online = (
            last_updated and 
            timezone.now() - last_updated < timedelta(minutes=15)
        )

        response_data = {
            'status': 'online' if is_online else 'offline',
            'last_updated': last_updated,
            'status_data': status_data
        }

        serializer = CharacterStatusResponseSerializer(response_data)
        return Response(serializer.data)
    except Exception as e:
        logger.error(f"获取状态失败: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        ) 