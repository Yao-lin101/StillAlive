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
import json
import urllib.request
import urllib.error
import os

logger = logging.getLogger(__name__)

# ip2region 本地库支持
XDB_SEARCHER = None
XDB_PATH = os.path.join(settings.BASE_DIR, 'data', 'ip2region.xdb')

try:
    from ip2region import searcher as ip2region_searcher
    from ip2region.util import IPv4 as IP2REGION_IPV4
    # 检查文件是否存在且大小合理 (>100KB 才是有效的 xdb 文件)
    if os.path.exists(XDB_PATH) and os.path.getsize(XDB_PATH) > 100000:
        XDB_SEARCHER = ip2region_searcher.new_with_file_only(IP2REGION_IPV4, XDB_PATH)
        logger.info(f"ip2region initialized with {XDB_PATH}")
    else:
        logger.warning(f"ip2region.xdb not found or invalid at {XDB_PATH}")
except ImportError as e:
    logger.warning(f"ip2region not installed: {e}")
except Exception as e:
    logger.error(f"Failed to initialize ip2region: {e}")

from apps.characters.models import Character, CharacterStatus, WillConfig, Message
from apps.characters.serializers import (
    CharacterSerializer, CharacterDetailSerializer, CharacterDisplaySerializer,
    CharacterStatusUpdateSerializer, CharacterStatusResponseSerializer,
    WillConfigSerializer, MessageSerializer
)

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
        """返回所有激活且公开的角色"""
        return Character.objects.filter(is_active=True, is_public=True).order_by('-updated_at')
    
    def list(self, request, *args, **kwargs):
        """获取所有存活者及其状态"""
        queryset = self.get_queryset()
        survivors = []
        
        for character in queryset:
            # 获取角色最新状态
            latest_statuses = CharacterStatus.get_latest_status(character)
            
            # 获取最新更新时间,非定时更新
            last_updated = None
            for status_item in latest_statuses:
                if status_item.status_type != 'other':
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
                'experience': character.experience,
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
    from django.core.cache import cache
    
    RATE_LIMIT_UPLOADS = 250  # 每小时最大上传次数
    RATE_LIMIT_WINDOW = 3600  # 1小时（秒）
    
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
        
        # 速率限制检查
        rate_limit_key = f"status_upload_rate:{character.uid}"
        current_count = cache.get(rate_limit_key, 0)
        
        if current_count >= RATE_LIMIT_UPLOADS:
            return Response(
                {'error': f'已超过每小时 {RATE_LIMIT_UPLOADS} 次的上传限制'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
        
        # 创建新状态记录
        CharacterStatus.objects.create(
            character=character,
            status_type=serializer.validated_data['type'],
            data=serializer.validated_data['data']
        )
        
        # 经验值系统 - 连续同步奖励
        from datetime import date, timedelta
        today = date.today()
        
        if character.last_sync_date != today:
            # 今天还没有获得同步经验
            if character.last_sync_date == today - timedelta(days=1):
                # 连续同步，streak +1
                character.sync_streak += 1
            else:
                # 断签，重置为1
                character.sync_streak = 1
            
            # 获得经验 = 当前连续天数
            character.experience += character.sync_streak
            character.last_sync_date = today
            character.save(update_fields=['experience', 'sync_streak', 'last_sync_date'])
        
        # 更新计数器
        if current_count == 0:
            cache.set(rate_limit_key, 1, RATE_LIMIT_WINDOW)
        else:
            cache.incr(rate_limit_key)

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
class CharacterMessageView(generics.ListCreateAPIView):
    """
    角色留言板 API
    GET: 获取最近50条留言
    POST: 发送新留言
    """
    serializer_class = MessageSerializer
    permission_classes = [AllowAny]
    pagination_class = None

    def get_queryset(self):
        code = self.kwargs.get('code')
        character = get_object_or_404(Character, display_code=code, is_active=True)
        # 限制只返回最近50条
        return Message.objects.filter(character=character)[:50]

    def get_location_from_ip(self, ip):
        """通过本地ip2region库获取IP归属地"""
        if not ip or ip in ['127.0.0.1', 'localhost', '::1']:
            return '本地'
        
        # 优先使用本地 ip2region 库
        if XDB_SEARCHER is not None:
            try:
                result = XDB_SEARCHER.search(ip)
                
                if result:
                    # ip2region 返回格式: "国家|省份|城市|ISP" (4个字段)
                    parts = result.split('|')
                    province = parts[1] if len(parts) > 1 and parts[1] != '0' else ''
                    
                    # 只显示省份，去掉"省"后缀
                    if province:
                        province = province.replace('省', '').replace('市', '')
                        return province
                    return None
            except Exception as e:
                logger.error(f"ip2region lookup failed for {ip}: {e}")
        
        # 回退到在线 API
        try:
            url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
            with urllib.request.urlopen(url, timeout=2) as response:
                data = json.loads(response.read().decode())
                if data.get('status') == 'success':
                    region = data.get('regionName', '')
                    city = data.get('city', '')
                    if region == city:
                        return region
                    return f"{region} {city}".strip()
        except Exception as e:
            logger.error(f"Online IP lookup failed for {ip}: {e}")
            
        return None

    def perform_create(self, serializer):
        code = self.kwargs.get('code')
        character = get_object_or_404(Character, display_code=code, is_active=True)
        
        # 获取客户端IP
        x_forwarded_for = self.request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = self.request.META.get('REMOTE_ADDR')
            
        location = self.get_location_from_ip(ip) if ip else None
            
        serializer.save(character=character, ip_address=ip, location=location)
        
        # 经验值系统 - 弹幕贡献奖励
        from datetime import date
        today = date.today()
        
        if ip:
            # 检查是否需要重置今日IP列表
            if character.danmaku_ips_date != today:
                character.danmaku_ips_today = []
                character.danmaku_ips_date = today
            
            # 检查该IP今天是否已贡献过经验
            if ip not in character.danmaku_ips_today:
                character.danmaku_ips_today.append(ip)
                character.experience += 1
                character.save(update_fields=['experience', 'danmaku_ips_today', 'danmaku_ips_date'])

class CharacterMessageDetailView(generics.DestroyAPIView):
    """
    删除特定留言
    """
    queryset = Message.objects.all()
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # 仅允许删除属于自己角色的留言
        return Message.objects.filter(character__user=self.request.user)


class DailyReportConfigViewSet(viewsets.ModelViewSet):
    """
    日报配置管理 API
    
    提供日报配置的创建、查询、更新、删除等功能
    """
    permission_classes = [IsAuthenticated]
    serializer_class = 'DailyReportConfigSerializer'

    def get_serializer_class(self):
        from apps.characters.serializers import DailyReportConfigSerializer
        return DailyReportConfigSerializer

    def get_queryset(self):
        """只返回当前用户角色的日报配置"""
        from apps.characters.models import DailyReportConfig
        return DailyReportConfig.objects.filter(character__user=self.request.user)

    def get_object(self):
        """获取指定角色的日报配置"""
        from apps.characters.models import DailyReportConfig
        character_uid = self.kwargs.get('character_pk')
        return get_object_or_404(
            DailyReportConfig,
            character__uid=character_uid,
            character__user=self.request.user
        )

    def perform_create(self, serializer):
        """创建日报配置时关联到指定角色"""
        character = get_object_or_404(
            Character,
            uid=self.kwargs['character_pk'],
            user=self.request.user
        )
        serializer.save(character=character)
        
    def list(self, request, *args, **kwargs):
        """获取指定角色的日报配置，如果不存在则返回默认配置"""
        try:
            character_uid = self.kwargs.get('character_pk')
            instance = self.get_object()
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
        except Http404:
            # 如果不存在，返回默认配置
            from apps.characters.models import DailyReportConfig
            return Response({
                'is_enabled': False,
                'visibility': 'private',
                'field_mappings': {},
                'created_at': None,
                'updated_at': None
            })
        except Exception as e:
            logger.error(f"获取日报配置失败: {str(e)}")
            return Response(
                {'error': f'获取日报配置失败: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

    def create(self, request, *args, **kwargs):
        """创建或更新日报配置"""
        character_uid = self.kwargs.get('character_pk')
        logger.info(f"创建或更新角色 {character_uid} 的日报配置")
        
        try:
            # 尝试获取现有配置
            instance = self.get_object()
            
            # 如果存在，则更新
            serializer = self.get_serializer(instance, data=request.data, partial=True)
            if not serializer.is_valid():
                logger.error(f"更新日报配置验证失败: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
            self.perform_update(serializer)
            return Response(serializer.data)
        except Http404:
            # 如果不存在，则创建新配置
            serializer = self.get_serializer(data=request.data)
            if not serializer.is_valid():
                logger.error(f"创建日报配置验证失败: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
            self.perform_create(serializer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"更新日报配置失败: {str(e)}")
            return Response(
                {'error': f'更新日报配置失败: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )


@api_view(['GET'])
@permission_classes([AllowAny])
def get_daily_report_dates(request, code):
    """
    获取指定角色某月有日报的日期列表
    
    GET /api/v1/d/<code>/reports/dates/?year=2026&month=4
    """
    from apps.characters.models import DailyReport, DailyReportConfig
    
    year = request.query_params.get('year')
    month = request.query_params.get('month')
    
    if not year or not month:
        return Response(
            {'error': '缺少 year 或 month 参数'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        year = int(year)
        month = int(month)
    except (TypeError, ValueError):
        return Response(
            {'error': 'year 和 month 必须是整数'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    character = get_object_or_404(Character, display_code=code, is_active=True)
    is_owner = request.user.is_authenticated and character.user == request.user
    
    # 检查日报配置的可见性
    try:
        config = DailyReportConfig.objects.get(character=character)
        if not config.is_enabled:
            return Response({'dates': []})
        
        # 如果不是所有者且配置是私有的，返回空
        if not is_owner and config.visibility == 'private':
            return Response({'dates': []})
    except DailyReportConfig.DoesNotExist:
        return Response({'dates': []})
    
    # 查询该月的日报
    from datetime import date
    from calendar import monthrange
    
    _, days_in_month = monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, days_in_month)
    
    queryset = DailyReport.objects.filter(
        character=character,
        date__gte=start_date,
        date__lte=end_date
    )
    
    # 如果不是所有者，过滤掉隐藏的日报
    if not is_owner:
        queryset = queryset.filter(is_hidden=False)
    
    dates = [report.date.day for report in queryset]
    
    return Response({'dates': dates})


@api_view(['GET'])
@permission_classes([AllowAny])
def get_daily_report_detail(request, code):
    """
    获取指定日期的日报详情
    
    GET /api/v1/d/<code>/reports/detail/?date=2026-04-27
    """
    from apps.characters.models import DailyReport, DailyReportConfig
    from datetime import datetime
    
    date_str = request.query_params.get('date')
    
    if not date_str:
        return Response(
            {'error': '缺少 date 参数'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        report_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return Response(
            {'error': 'date 格式必须是 YYYY-MM-DD'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    character = get_object_or_404(Character, display_code=code, is_active=True)
    is_owner = request.user.is_authenticated and character.user == request.user
    
    # 检查日报配置的可见性
    try:
        config = DailyReportConfig.objects.get(character=character)
        if not config.is_enabled:
            return Response(
                {'error': '该角色未启用日报分析'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # 如果不是所有者且配置是私有的，返回404
        if not is_owner and config.visibility == 'private':
            return Response(
                {'error': '日报不存在'},
                status=status.HTTP_404_NOT_FOUND
            )
    except DailyReportConfig.DoesNotExist:
        return Response(
            {'error': '该角色未启用日报分析'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    # 查询日报
    report = get_object_or_404(
        DailyReport,
        character=character,
        date=report_date
    )
    
    # 如果不是所有者且日报被隐藏，返回404
    if not is_owner and report.is_hidden:
        return Response(
            {'error': '日报不存在'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    from apps.characters.serializers import DailyReportDetailSerializer
    serializer = DailyReportDetailSerializer(report)
    return Response(serializer.data)


@api_view(['GET'])
@permission_classes([AllowAny])
def get_daily_report_config_public(request, code):
    """
    公开获取日报配置（用于展示页判断是否显示日报分析页签）
    
    GET /api/v1/d/<code>/reports/config/
    """
    from apps.characters.models import DailyReportConfig
    
    character = get_object_or_404(Character, display_code=code, is_active=True)
    is_owner = request.user.is_authenticated and character.user == request.user
    
    try:
        config = DailyReportConfig.objects.get(character=character)
        
        # 如果未启用，返回 is_enabled: false
        if not config.is_enabled:
            return Response({
                'is_enabled': False,
                'is_visible': False
            })
        
        # 如果是私有的且当前用户不是所有者，返回不可见
        if config.visibility == 'private' and not is_owner:
            return Response({
                'is_enabled': True,
                'is_visible': False,
                'visibility': config.visibility
            })
        
        # 可见
        return Response({
            'is_enabled': True,
            'is_visible': True,
            'visibility': config.visibility,
            'is_owner': is_owner
        })
    except DailyReportConfig.DoesNotExist:
        return Response({
            'is_enabled': False,
            'is_visible': False
        })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def toggle_daily_report_hidden(request, character_uid):
    """
    隐藏/取消隐藏日报（仅角色所有者可操作）
    
    POST /api/v1/characters/<uid>/reports/toggle-hidden/
    Body: {"date": "2026-04-27", "is_hidden": true}
    """
    from apps.characters.models import DailyReport
    from datetime import datetime
    
    character = get_object_or_404(
        Character,
        uid=character_uid,
        user=request.user
    )
    
    date_str = request.data.get('date')
    is_hidden = request.data.get('is_hidden')
    
    if not date_str or is_hidden is None:
        return Response(
            {'error': '缺少 date 或 is_hidden 参数'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        report_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return Response(
            {'error': 'date 格式必须是 YYYY-MM-DD'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    report = get_object_or_404(
        DailyReport,
        character=character,
        date=report_date
    )
    
    report.is_hidden = is_hidden
    report.save(update_fields=['is_hidden'])
    
    return Response({
        'date': date_str,
        'is_hidden': report.is_hidden
    })


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_daily_report(request, character_uid):
    """
    删除日报（仅角色所有者可操作）
    
    DELETE /api/v1/characters/<uid>/reports/delete/?date=2026-04-27
    """
    from apps.characters.models import DailyReport
    from datetime import datetime
    
    character = get_object_or_404(
        Character,
        uid=character_uid,
        user=request.user
    )
    
    date_str = request.query_params.get('date')
    
    if not date_str:
        return Response(
            {'error': '缺少 date 参数'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        report_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return Response(
            {'error': 'date 格式必须是 YYYY-MM-DD'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    report = get_object_or_404(
        DailyReport,
        character=character,
        date=report_date
    )
    
    report.delete()
    
    return Response({'status': 'success', 'message': '日报已删除'})


@api_view(['POST'])
@permission_classes([AllowAny])
def sync_external_status(request):
    """
    外部状态同步接口
    
    接收外部系统（如QQ机器人）发送的状态数据
    
    POST /api/v1/status/sync/
    Headers: X-Character-Key: <secret_key>
    Body:
    {
        "type": "qq_messages",
        "data": {
            "private_messages": [
                {
                    "时间": "12:00",
                    "用户昵称": "消息内容",
                    "机器人昵称": "回复消息"
                }
            ],
            "group_messages": [
                {
                    "时间": "12:00",
                    "群名称": "群名称",
                    "群友昵称A": "消息内容",
                    "用户昵称": "消息内容",
                    "群友昵称B": "消息内容"
                }
            ]
        }
    }
    """
    from django.core.cache import cache
    from apps.characters.models import DailyReport, QQMessage
    from datetime import date, datetime
    
    RATE_LIMIT_UPLOADS = 250  # 每小时最大上传次数
    RATE_LIMIT_WINDOW = 3600  # 1小时（秒）
    
    try:
        # 从请求头获取秘钥
        secret_key = request.headers.get('X-Character-Key')
        if not secret_key:
            return Response(
                {'error': '缺少认证秘钥'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # 验证请求数据
        if not request.data:
            return Response(
                {'error': '缺少请求数据'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 验证 secret_key
        character = get_object_or_404(Character, secret_key=secret_key)
        
        # 速率限制检查
        rate_limit_key = f"status_upload_rate:{character.uid}"
        current_count = cache.get(rate_limit_key, 0)
        
        if current_count >= RATE_LIMIT_UPLOADS:
            return Response(
                {'error': f'已超过每小时 {RATE_LIMIT_UPLOADS} 次的上传限制'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
        
        # 处理QQ消息数据
        if 'data' in request.data and request.data['type'] == 'qq_messages':
            qq_data = request.data['data']
            today = date.today()
            
            # 处理私聊消息
            if 'private_messages' in qq_data and qq_data['private_messages']:
                private_messages = qq_data['private_messages']
                
                # 查找今天的私聊消息记录
                existing_private = QQMessage.objects.filter(
                    character=character,
                    date=today,
                    message_type='private'
                ).first()
                
                if existing_private:
                    # 增量更新
                    existing_data = existing_private.message_data
                    existing_data.extend(private_messages)
                    existing_private.message_data = existing_data
                    existing_private.save()
                else:
                    # 创建新记录
                    QQMessage.objects.create(
                        character=character,
                        date=today,
                        message_type='private',
                        message_data=private_messages
                    )
            
            # 处理群消息
            if 'group_messages' in qq_data and qq_data['group_messages']:
                group_messages = qq_data['group_messages']
                
                # 查找今天的群消息记录
                existing_group = QQMessage.objects.filter(
                    character=character,
                    date=today,
                    message_type='group'
                ).first()
                
                if existing_group:
                    # 增量更新
                    existing_data = existing_group.message_data
                    existing_data.extend(group_messages)
                    existing_group.message_data = existing_data
                    existing_group.save()
                else:
                    # 创建新记录
                    QQMessage.objects.create(
                        character=character,
                        date=today,
                        message_type='group',
                        message_data=group_messages
                    )
        
        # 经验值系统 - 连续同步奖励
        from datetime import date, timedelta
        today = date.today()
        
        if character.last_sync_date != today:
            # 今天还没有获得同步经验
            if character.last_sync_date == today - timedelta(days=1):
                # 连续同步，streak +1
                character.sync_streak += 1
            else:
                # 断签，重置为1
                character.sync_streak = 1
            
            # 获得经验 = 当前连续天数
            character.experience += character.sync_streak
            character.last_sync_date = today
            character.save(update_fields=['experience', 'sync_streak', 'last_sync_date'])
        
        # 更新计数器
        if current_count == 0:
            cache.set(rate_limit_key, 1, RATE_LIMIT_WINDOW)
        else:
            cache.incr(rate_limit_key)

        return Response({'status': 'success'})
    except Exception as e:
        logger.error(f"同步外部状态失败: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )
