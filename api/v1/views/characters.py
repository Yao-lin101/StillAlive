from rest_framework import viewsets, status, generics
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.http import Http404
from datetime import timedelta
from django.core.mail import EmailMessage
from django.conf import settings
from django.template.loader import render_to_string
import logging

from apps.characters.models import Character, CharacterStatus, WillConfig
from apps.characters.serializers import (
    CharacterSerializer, CharacterDetailSerializer, CharacterDisplaySerializer,
    CharacterStatusUpdateSerializer, CharacterStatusResponseSerializer,
    WillConfigSerializer
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
    
    def create(self, request, *args, **kwargs):
        """创建角色时进行额外验证"""
        if not request.user.is_superuser:
            character_count = Character.objects.filter(user=request.user).count()
            if character_count >= 4:
                return Response(
                    {'error': '算你厉害，但是也别太贪心了'},
                    status=status.HTTP_403_FORBIDDEN
                )
        return super().create(request, *args, **kwargs)
    
    def get_serializer_class(self):
        """根据操作类型返回不同的序列化器"""
        if self.action in ['retrieve', 'update', 'partial_update', 'create']:
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

    def update(self, request, *args, **kwargs):
        """更新角色信息"""
        try:
            response = super().update(request, *args, **kwargs)
            return response
        except Exception as e:
            logger.error(f"Error updating character {kwargs.get('pk')}: {str(e)}")
            return Response({"detail": str(e)}, status=400)

class SurvivorsListView(generics.ListAPIView):
    """公开访问的存活者列表视图 - Survivors 页面"""
    serializer_class = CharacterDisplaySerializer
    permission_classes = [AllowAny]
    
    def get_queryset(self):
        """返回所有激活状态的角色"""
        return Character.objects.filter(is_active=True).order_by('-updated_at')
    
    def list(self, request, *args, **kwargs):
        """获取所有存活者及其状态"""
        queryset = self.get_queryset()
        survivors = []
        
        for character in queryset:
            # 获取角色最新状态
            latest_statuses = CharacterStatus.get_latest_status(character)
            
            # 获取最新更新时间
            last_updated = None
            for status_item in latest_statuses:
                if status_item.status_type == 'vital_signs':
                    last_updated = status_item.timestamp
                    break
            
            # 如果没有 vital_signs，使用任意最新状态的时间
            if not last_updated and latest_statuses:
                last_updated = latest_statuses[0].timestamp
            
            # 判断在线状态（15分钟内更新为在线）
            is_online = (
                last_updated and 
                timezone.now() - last_updated < timedelta(minutes=15)
            )
            
            # 获取状态消息
            status_message = ""
            if character.status_config and 'display' in character.status_config:
                display_config = character.status_config['display']
                if last_updated:
                    diff_hours = (timezone.now() - last_updated).total_seconds() / 3600
                    timeout_messages = display_config.get('timeout_messages', [])
                    # 按小时降序排序
                    timeout_messages_sorted = sorted(timeout_messages, key=lambda x: x.get('hours', 0), reverse=True)
                    for msg in timeout_messages_sorted:
                        if diff_hours >= msg.get('hours', 0):
                            status_message = msg.get('message', '')
                            break
                    if not status_message:
                        status_message = display_config.get('default_message', '')
                else:
                    status_message = display_config.get('default_message', '')
            
            survivors.append({
                'display_code': character.display_code,
                'name': character.name,
                'avatar': character.avatar,
                'bio': character.bio,
                'is_online': is_online,
                'last_updated': last_updated,
                'status_message': status_message,
            })
        
        return Response({
            'count': len(survivors),
            'results': survivors
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
        
        try:
            character = queryset.get(display_code=code)
            if not character.is_active:
                logger.info(f"Character {code} is inactive")
                raise Character.DoesNotExist("该角色已被禁用")
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

class WillConfigViewSet(viewsets.ModelViewSet):
    """
    遗嘱配置管理 API
    
    提供遗嘱配置的创建、查询、更新、删除等功能
    """
    permission_classes = [IsAuthenticated]
    serializer_class = WillConfigSerializer

    def get_queryset(self):
        """只返回当前用户角色的遗嘱配置"""
        return WillConfig.objects.filter(character__user=self.request.user)

    def get_object(self):
        """获取指定角色的遗嘱配置"""
        character_uid = self.kwargs.get('character_pk')
        return get_object_or_404(
            WillConfig,
            character__uid=character_uid,
            character__user=self.request.user
        )

    def perform_create(self, serializer):
        """创建遗嘱配置时关联到指定角色"""
        character = get_object_or_404(
            Character,
            uid=self.kwargs['character_pk'],
            user=self.request.user
        )
        serializer.save(character=character)
        
    def list(self, request, *args, **kwargs):
        """获取指定角色的遗嘱配置，如果不存在则返回404"""
        try:
            character_uid = self.kwargs.get('character_pk')
            instance = self.get_object()
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"获取遗嘱配置失败: {str(e)}")
            # 如果不存在，返回404
            return Response(
                {'error': '遗嘱配置不存在'},
                status=status.HTTP_404_NOT_FOUND
            )

    def create(self, request, *args, **kwargs):
        """创建或更新遗嘱配置"""
        character_uid = self.kwargs.get('character_pk')
        logger.info(f"创建或更新角色 {character_uid} 的遗嘱配置")
        
        try:
            # 尝试获取现有配置
            instance = self.get_object()
            
            # 如果存在，则更新
            serializer = self.get_serializer(instance, data=request.data, partial=True)
            if not serializer.is_valid():
                logger.error(f"更新遗嘱配置验证失败: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
            self.perform_update(serializer)
            return Response(serializer.data)
        except Http404:
            # 如果不存在，则创建新配置
            serializer = self.get_serializer(data=request.data)
            if not serializer.is_valid():
                logger.error(f"创建遗嘱配置验证失败: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
            self.perform_create(serializer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"更新遗嘱配置失败: {str(e)}")
            return Response(
                {'error': f'更新遗嘱配置失败: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            ) 