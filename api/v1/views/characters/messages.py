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


