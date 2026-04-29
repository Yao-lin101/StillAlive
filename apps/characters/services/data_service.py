import logging
from django.utils import timezone
from datetime import timedelta, datetime
from collections import defaultdict, Counter
from apps.characters.models import CharacterStatus

logger = logging.getLogger(__name__)

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
    
    aggregated = {
        'date': target_date.isoformat(),
        'total_records': statuses.count(),
        'last_record_time': timezone.localtime(latest_status.timestamp).isoformat() if latest_status else None,
        'data_cutoff_time': timezone.localtime(end_datetime).isoformat() if timezone.is_aware(end_datetime) else end_datetime.isoformat(),
        'phone_app_usage': [],
        'computer_app_usage': [],
        'steps_data': [],
        'all_statuses': []
    }
    
    phone_key = field_mappings.get('phone_app')
    computer_key = field_mappings.get('computer_app')
    steps_key = field_mappings.get('steps')
    
    for status in statuses:
        local_timestamp = timezone.localtime(status.timestamp)
        
        status_data = {
            'timestamp': local_timestamp.isoformat(),
            'status_type': status.status_type,
            'data': status.data
        }
        aggregated['all_statuses'].append(status_data)
        
        data = status.data
        hour = local_timestamp.hour
        
        if phone_key and phone_key in data:
            value = data[phone_key]
            if value:
                aggregated['phone_app_usage'].append({
                    'hour': hour,
                    'timestamp': local_timestamp.isoformat(),
                    'app': str(value)
                })
        
        if computer_key and computer_key in data:
            value = data[computer_key]
            if value:
                aggregated['computer_app_usage'].append({
                    'hour': hour,
                    'timestamp': local_timestamp.isoformat(),
                    'app': str(value)
                })
        
        if steps_key and steps_key in data:
            try:
                steps = int(data[steps_key])
                aggregated['steps_data'].append({
                    'hour': hour,
                    'timestamp': local_timestamp.isoformat(),
                    'steps': steps
                })
            except (ValueError, TypeError):
                pass
    
    if aggregated['phone_app_usage']:
        phone_counter = Counter(item['app'] for item in aggregated['phone_app_usage'])
        aggregated['phone_app_summary'] = dict(phone_counter.most_common(20))
        
        hourly_phone = defaultdict(list)
        for item in aggregated['phone_app_usage']:
            hourly_phone[item['hour']].append(item['app'])
        aggregated['phone_app_by_hour'] = {
            str(hour): dict(Counter(apps).most_common(5))
            for hour, apps in hourly_phone.items()
        }
    
    if aggregated['computer_app_usage']:
        computer_counter = Counter(item['app'] for item in aggregated['computer_app_usage'])
        aggregated['computer_app_summary'] = dict(computer_counter.most_common(20))
        
        hourly_computer = defaultdict(list)
        for item in aggregated['computer_app_usage']:
            hourly_computer[item['hour']].append(item['app'])
        aggregated['computer_app_by_hour'] = {
            str(hour): dict(Counter(apps).most_common(5))
            for hour, apps in hourly_computer.items()
        }
    
    if aggregated['steps_data']:
        steps_values = [item['steps'] for item in aggregated['steps_data']]
        aggregated['steps_summary'] = {
            'total': steps_values[-1] if steps_values else 0
        }
        
        hourly_steps = defaultdict(list)
        for item in aggregated['steps_data']:
            hourly_steps[item['hour']].append(item['steps'])
        aggregated['steps_by_hour'] = {
            str(hour): max(steps) if steps else 0
            for hour, steps in hourly_steps.items()
        }
    
    active_hours = set()
    for status in statuses:
        active_hours.add(timezone.localtime(status.timestamp).hour)
    aggregated['active_hours'] = sorted(list(active_hours))
    
    if aggregated['active_hours']:
        aggregated['first_activity_hour'] = min(aggregated['active_hours'])
        aggregated['last_activity_hour'] = max(aggregated['active_hours'])
        
    # 为了防止 raw_data 过大撑爆数据库并浪费 token，在此处删除用于计算的明细流水数组
    aggregated.pop('all_statuses', None)
    aggregated.pop('phone_app_usage', None)
    aggregated.pop('computer_app_usage', None)
    aggregated.pop('steps_data', None)
    
    return aggregated


