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


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def bot_query_state(request):
    """
    提供给机器人的接口，用于查询当前角色的实时状态并直接返回组合好的 prompt。
    支持三种模式（通过 memory 查询参数指定）：
    - short: 仅返回今日实时数据（默认）
    - long: 仅返回长期记忆（需提供 q 查询参数）
    - hybrid: 结合实时数据和相关长期记忆
    """
    try:
        secret_key = request.headers.get('X-Character-Key') or request.headers.get('Authorization', '').replace('Bearer ', '')
        if not secret_key:
            return Response({'error': '缺少认证秘钥'}, status=status.HTTP_401_UNAUTHORIZED)
            
        character = get_object_or_404(Character, secret_key=secret_key)
        
        memory_mode = request.query_params.get('memory', 'short')
        query_str = request.query_params.get('q', '')
        
        from apps.characters.models import DailyReportConfig
        try:
            config = DailyReportConfig.objects.get(character=character)
            if not config.is_enabled:
                return Response({'error': '未开启日报配置'}, status=status.HTTP_400_BAD_REQUEST)
        except DailyReportConfig.DoesNotExist:
            return Response({'error': '日报配置不存在'}, status=status.HTTP_400_BAD_REQUEST)
            
        from apps.characters.services.data_service import aggregate_status_data
        from apps.characters.services.llm_utils import build_data_section
        from apps.characters.services.important_event_service import (
            retrieve_important_events, format_events_for_prompt, _search_milvus_event_ids, ImportantEvent
        )
        
        now = timezone.localtime(timezone.now())
        target_date = now.date()
        
        prompt_content = ""
        aggregated_data = {}
        
        if memory_mode in ['short', 'hybrid']:
            # 获取实时数据
            aggregated_data = aggregate_status_data(
                character, 
                config.field_mappings or {}, 
                target_date,
                end_datetime=now
            )
            
            if aggregated_data:
                # 剔除 QQ 聊天等敏感或冗长数据
                aggregated_data.pop('qq_messages', None)
                aggregated_data.pop('qq_messages_summary', None)
                
                cutoff_time_str = aggregated_data.get('data_cutoff_time', '未知')
                target_date_str = aggregated_data.get('date', '')
                weekday_str = ""
                if target_date_str:
                    try:
                        target_date_obj = timezone.datetime.fromisoformat(target_date_str).date()
                        weekday_map = {0: '星期一', 1: '星期二', 2: '星期三', 3: '星期四', 4: '星期五', 5: '星期六', 6: '星期日'}
                        weekday_str = f" ({weekday_map[target_date_obj.weekday()]})"
                    except Exception:
                        pass
                        
                prompt_content += build_data_section(
                    aggregated_data, 
                    f"{target_date_str}{weekday_str}", 
                    cutoff_time_str, 
                    is_day_ended=False
                )
                
                system_inferred_persona = config.system_inferred_persona
                if system_inferred_persona and system_inferred_persona.strip():
                    prompt_content += f"\n\n## 你观察得出的真实侧写档案\n{system_inferred_persona.strip()}"
        else:
            # long 模式下，不需要跑一遍完整的 aggregate_status_data
            aggregated_data = {'date': target_date.isoformat()}
            
        if memory_mode in ['long', 'hybrid']:
            events = []
            if query_str:
                # 用户主动指定的查询，直接检索 Milvus
                vector_scores = _search_milvus_event_ids(character, query_str, recall=10)
                if vector_scores:
                    events = list(ImportantEvent.objects.filter(id__in=vector_scores.keys(), is_active=True))
                    # 按照相关性排序
                    events.sort(key=lambda e: vector_scores.get(e.id, 0), reverse=True)
            else:
                # 如果没有 query_str，走自动召回逻辑
                events = retrieve_important_events(character, aggregated_data)
                
            if events:
                memory_text = format_events_for_prompt(events, for_bot=True)
                # 确保段落之间有空行
                if prompt_content:
                    prompt_content += f"\n\n{memory_text}\n"
                else:
                    prompt_content += f"{memory_text}\n"

        return Response({'prompt': prompt_content.strip()})
        
    except Exception as e:
        logger.error(f"Bot query state failed: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
