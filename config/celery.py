from __future__ import absolute_import, unicode_literals
import os
from celery import Celery
from celery.schedules import crontab

# 设置 Django 默认设置模块
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')

app = Celery('config')

# 添加新的配置
app.conf.broker_connection_retry_on_startup = True

# 使用 Django 的设置文件配置 Celery
app.config_from_object('django.conf:settings', namespace='CELERY')

# 自动从所有已注册的 Django app 中加载任务
app.autodiscover_tasks()

# 配置定时任务
app.conf.beat_schedule = {
    'check-wills-hourly': {
        'task': 'apps.characters.tasks.check_wills',
        'schedule': crontab(minute=0),  # 每小时执行一次
    },
    'generate-daily-reports': {
        'task': 'apps.characters.tasks.generate_daily_reports',
        'schedule': crontab(minute=5),  # 每小时第 5 分钟执行（总结昨日或更新今日）
    },
    'update-personas': {
        'task': 'apps.characters.tasks.update_personas',
        'schedule': crontab(hour=0, minute=20),  # 每天 00:20 独立执行侧写更新
    },
    'generate-important-event-memories': {
        'task': 'apps.characters.tasks.generate_important_event_memories',
        'schedule': crontab(hour=0, minute=40),  # 每天 00:40 抽取昨天的重要事件（推后 10 分钟以避开高峰）
    },
}

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}') 
