import logging
from django.utils import timezone
from datetime import timedelta, datetime
from collections import defaultdict, Counter
from apps.characters.models import CharacterStatus

logger = logging.getLogger(__name__)

def _extract_raw_usage(statuses, field_mappings):
    """提取手机应用、电脑应用和步数的原始流水，以及当天的活跃小时集合"""
    phone_key = field_mappings.get('phone_app')
    computer_key = field_mappings.get('computer_app')
    steps_key = field_mappings.get('steps')
    
    phone_app_usage = []
    computer_app_usage = []
    steps_data = []
    active_hours = set()
    
    for status in statuses:
        local_timestamp = timezone.localtime(status.timestamp)
        data = status.data
        hour = local_timestamp.hour
        active_hours.add(hour)
        
        if phone_key and phone_key in data:
            value = data[phone_key]
            if value:
                phone_app_usage.append({
                    'hour': hour,
                    'app': str(value)
                })
        
        if computer_key and computer_key in data:
            value = data[computer_key]
            if value:
                computer_app_usage.append({
                    'hour': hour,
                    'app': str(value)
                })
        
        if steps_key and steps_key in data:
            try:
                steps_data.append({
                    'hour': hour,
                    'steps': int(data[steps_key])
                })
            except (ValueError, TypeError):
                pass
                
    return phone_app_usage, computer_app_usage, steps_data, sorted(list(active_hours))


def _compute_app_summary(app_usage):
    """计算应用的汇总排行和按小时排行"""
    if not app_usage:
        return None, None
        
    counter = Counter(item['app'] for item in app_usage)
    summary = dict(counter.most_common(20))
    
    hourly = defaultdict(list)
    for item in app_usage:
        hourly[item['hour']].append(item['app'])
        
    by_hour = {
        str(hour): dict(Counter(apps).most_common(5))
        for hour, apps in hourly.items()
    }
    return summary, by_hour


def _compute_steps_summary(steps_data):
    """计算步数总计和按小时步数最大值"""
    if not steps_data:
        return None, None
        
    steps_values = [item['steps'] for item in steps_data]
    summary = {'total': steps_values[-1] if steps_values else 0}
    
    hourly = defaultdict(list)
    for item in steps_data:
        hourly[item['hour']].append(item['steps'])
        
    by_hour = {
        str(hour): max(steps) if steps else 0
        for hour, steps in hourly.items()
    }
    return summary, by_hour


def _get_historical_active_hours(character, start_time, end_time):
    """查询指定时间段内的活跃小时集合"""
    statuses = CharacterStatus.objects.filter(
        character=character,
        timestamp__gte=start_time,
        timestamp__lt=end_time
    ).values_list('timestamp', flat=True)
    
    active_hours = set()
    for ts in statuses:
        active_hours.add(timezone.localtime(ts).hour)
    return sorted(list(active_hours))


def aggregate_status_data(character, field_mappings, target_date, end_datetime=None):
    """
    聚合指定日期的状态数据
    
    Args:
        character: 角色对象
        field_mappings: 字段映射，格式: {"phone_app": "key", "computer_app": "key", "steps": "key"}
        target_date: 目标日期
        end_datetime: 数据截止时间（可选，默认是当天结束）
    
    Returns:
        dict: 聚合后的数据，包含 'last_record_time' 字段（最新状态的时间）
    """
    start_datetime = timezone.make_aware(datetime.combine(target_date, datetime.min.time()))
    if end_datetime is None:
        end_datetime = start_datetime + timedelta(days=1)
        
    statuses = CharacterStatus.objects.filter(
        character=character,
        timestamp__gte=start_datetime,
        timestamp__lt=end_datetime
    ).order_by('timestamp')
    
    if not statuses.exists():
        return None
        
    latest_status = statuses.last()
    
    phone_app_usage, computer_app_usage, steps_data, active_hours = _extract_raw_usage(statuses, field_mappings)
    
    phone_summary, phone_by_hour = _compute_app_summary(phone_app_usage)
    computer_summary, computer_by_hour = _compute_app_summary(computer_app_usage)
    steps_summary, steps_by_hour = _compute_steps_summary(steps_data)
    
    aggregated = {
        'date': target_date.isoformat(),
        'total_records': statuses.count(),
        'last_record_time': timezone.localtime(latest_status.timestamp).isoformat() if latest_status else None,
        'data_cutoff_time': timezone.localtime(end_datetime).isoformat() if timezone.is_aware(end_datetime) else end_datetime.isoformat(),
        
        'active_hours': active_hours,
        'first_activity_hour': min(active_hours) if active_hours else '未知',
        'last_activity_hour': max(active_hours) if active_hours else '未知',
    }
    
    if phone_summary:
        aggregated['phone_app_summary'] = phone_summary
        aggregated['phone_app_by_hour'] = phone_by_hour
        
    if computer_summary:
        aggregated['computer_app_summary'] = computer_summary
        aggregated['computer_app_by_hour'] = computer_by_hour
        
    if steps_summary:
        aggregated['steps_summary'] = steps_summary
        aggregated['steps_by_hour'] = steps_by_hour
        
    aggregated['yesterday_active_hours'] = _get_historical_active_hours(
        character, 
        start_datetime - timedelta(days=1), 
        start_datetime
    )
    
    aggregated['day_before_yesterday_active_hours'] = _get_historical_active_hours(
        character, 
        start_datetime - timedelta(days=2), 
        start_datetime - timedelta(days=1)
    )
    
    return aggregated


