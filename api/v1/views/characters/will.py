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
