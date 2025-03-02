from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.conf import settings
from .models import WillConfig, CharacterStatus
import logging
import os
from django.db import transaction

logger = logging.getLogger(__name__)

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=300,  # 5分钟后重试
    autoretry_for=(Exception,),
    retry_backoff=True,  # 使用指数退避算法
)
def send_will_email(self, will_config_id):
    """
    发送遗嘱邮件的异步任务
    """
    try:
        will_config = WillConfig.objects.select_related('character').get(id=will_config_id)
        
        # 获取最后更新时间
        last_status = CharacterStatus.objects.filter(
            character=will_config.character
        ).order_by('-timestamp').first()
        
        last_updated = last_status.timestamp if last_status else timezone.now()
        
        # 渲染邮件模板
        html_content = render_to_string('emails/will_notification.html', {
            'character_name': will_config.character.name,
            'content': will_config.content,
            'last_updated': last_updated.strftime('%Y-%m-%d %H:%M:%S')
        })

        # 创建邮件
        email = EmailMessage(
            subject=f"来自 {will_config.character.name} 的遗嘱",
            body=html_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[will_config.target_email],
            cc=will_config.cc_emails
        )
        email.content_subtype = "html"
        
        # 发送邮件
        email.send()
        
        logger.info(f"Will email sent successfully for character {will_config.character.name}")
        return True
    except Exception as e:
        logger.error(f"Failed to send will email: {str(e)}")
        raise self.retry(exc=e)

@shared_task
def check_wills():
    """
    定时检查是否需要发送遗嘱
    """
    now = timezone.now()
    logger.info("Starting will check task")
    
    # 获取所有启用了遗嘱功能的配置
    active_wills = WillConfig.objects.filter(
        is_enabled=True
    ).select_related('character')

    logger.info(f"Found {active_wills.count()} active wills")

    for will in active_wills:
        try:
            # 获取角色最后的状态更新时间
            last_status = CharacterStatus.objects.filter(
                character=will.character
            ).order_by('-timestamp').first()

            if not last_status:
                logger.info(f"No status found for character {will.character.name}")
                continue

            # 计算是否超过设定的超时时间
            timeout = timedelta(hours=will.timeout_hours)
            time_since_last_update = now - last_status.timestamp
            
            logger.info(f"Character: {will.character.name}")
            logger.info(f"Now: {now}")
            logger.info(f"Last status: {last_status.timestamp}")
            logger.info(f"Time since last update: {time_since_last_update}")
            logger.info(f"Timeout setting: {timeout}")
            
            if time_since_last_update > timeout:
                logger.info(
                    f"Timeout detected for character {will.character.name} "
                    f"(uid: {will.character.uid}). Last status update was {time_since_last_update} ago"
                )
                
                # 禁用遗嘱配置
                will.is_enabled = False
                will.save(update_fields=['is_enabled'])
                logger.info(f"Will config disabled: {will.is_enabled}")
                
                # 发送邮件通知
                send_will_email.delay(will.id)
                logger.info(f"Will config disabled for character {will.character.name}")
            else:
                logger.info(f"No timeout detected for character {will.character.name}")
            
        except Exception as e:
            logger.error(f"Error processing will for character {will.character.name}: {str(e)}")
            continue

    logger.info("Will check task completed") 