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

from apps.characters.models import Character, CharacterStatus, WillConfig, Message, DailyReportConfig, DailyReport, QQMessage
from apps.characters.serializers import (
    CharacterSerializer, CharacterDetailSerializer, CharacterDisplaySerializer,
    CharacterStatusUpdateSerializer, CharacterStatusResponseSerializer,
    WillConfigSerializer, MessageSerializer, DailyReportConfigSerializer, DailyReportDetailSerializer
)

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
    
    # 获取并注入模板风格
    res_data = serializer.data
    res_data['template_style'] = config.template_style if config else 'default'
    
    return Response(res_data)


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

