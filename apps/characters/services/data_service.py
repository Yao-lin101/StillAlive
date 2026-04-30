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
                    'app': str(value),
                    'timestamp': local_timestamp
                })
        
        if computer_key and computer_key in data:
            value = data[computer_key]
            if value:
                computer_app_usage.append({
                    'hour': hour,
                    'app': str(value),
                    'timestamp': local_timestamp
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


def _compute_app_duration(app_usage):
    """计算应用的使用时长（分钟），基于应用切换间隔"""
    if not app_usage:
        return None, None
    
    # 按时间排序
    sorted_usage = sorted(app_usage, key=lambda x: x['timestamp'])
    
    # 合并连续使用的同一个应用
    merged_usage = []
    for item in sorted_usage:
        if not merged_usage or merged_usage[-1]['app'] != item['app']:
            merged_usage.append(item)
    
    duration_summary = defaultdict(float)
    duration_by_hour = defaultdict(lambda: defaultdict(float))
    
    # 计算每个应用的使用时长
    for i, current in enumerate(merged_usage):
        if i < len(merged_usage) - 1:
            next_item = merged_usage[i + 1]
            duration = (next_item['timestamp'] - current['timestamp']).total_seconds() / 60
        else:
            # 最后一个应用，假设使用到下一个小时的开始
            next_hour = current['timestamp'].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            duration = (next_hour - current['timestamp']).total_seconds() / 60
        

        
        # 处理跨小时边界的情况
        current_hour = current['hour']
        current_time = current['timestamp']
        hour_end = current_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        
        if current_time + timedelta(minutes=duration) <= hour_end:
            # 完全在当前小时内
            duration_summary[current['app']] += duration
            duration_by_hour[current_hour][current['app']] += duration
        else:
            # 跨小时
            hours_duration = (hour_end - current_time).total_seconds() / 60
            duration_summary[current['app']] += duration
            duration_by_hour[current_hour][current['app']] += hours_duration
            
            # 计算下一个小时的时长
            next_hour_duration = duration - hours_duration
            next_hour_num = (current_hour + 1) % 24
            duration_by_hour[next_hour_num][current['app']] += next_hour_duration
    
    # 转换为普通字典
    summary = dict(duration_summary)
    by_hour = {
        str(hour): dict(apps)
        for hour, apps in duration_by_hour.items()
    }
    
    return summary, by_hour


def _compute_app_by_time_range(app_usage, end_datetime=None, other_usage=None):
    """按照应用使用结束点聚合数据，聚合窗口最小为1小时。对于超过5次记录的应用，聚合为字符串统计形式以节省Token。"""
    if not app_usage:
        return None
    
    # 按时间排序
    sorted_usage = sorted(app_usage, key=lambda x: x['timestamp'])
    
    # 确定数据的最晚结束时间（用于最后一个应用的时长计算）
    final_boundary = end_datetime if end_datetime else sorted_usage[-1]['timestamp'].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    
    # 计算时间范围聚合，窗口最小为1小时
    time_range_agg = {}
    
    i = 0
    while i < len(sorted_usage):
        current = sorted_usage[i]
        window_start = current['timestamp']
        window_apps = defaultdict(list)
        
        # 累计窗口内的应用，只要加入后不超过1小时就继续；如果是首个应用但自身超1小时也加入后结束。
        j = i
        while j < len(sorted_usage):
            app_item = sorted_usage[j]
            app_end_time = sorted_usage[j + 1]['timestamp'] if j < len(sorted_usage) - 1 else final_boundary
            
            # 初步计算时长
            app_duration = (app_end_time - app_item['timestamp']).total_seconds() / 60
            
            if other_usage and app_duration >= 30.0:
                other_events = [x['timestamp'] for x in other_usage if app_item['timestamp'] < x['timestamp'] < app_end_time]
                
                # 长线任务超过30分钟，且被另一端截断3次以上，直接以第一次介入点为准
                if len(other_events) >= 3:
                    app_end_time = other_events[0]
                    app_duration = (app_end_time - app_item['timestamp']).total_seconds() / 60
            
            # 清理掉超过3小时(180分钟)的异常挂机任务（如睡着没关应用）
            if app_duration >= 180.0:
                window_end = app_item['timestamp'] if j > i else app_end_time
                j += 1
                break
            
            # 预计算加入当前应用后的窗口总时长
            projected_window_duration = (app_end_time - window_start).total_seconds() / 60
            
            # 核心修正：如果加入这个应用会超出60分钟，并且窗口里已经有其他应用了，把它留到下一个窗口
            if projected_window_duration > 80 and j > i:
                window_end = app_item['timestamp']
                break
                
            # 否则加入当前窗口
            window_apps[app_item['app']].append(round(app_duration, 1))
            
            # 如果加入后刚好达到或超过 60 分钟（比如它是当前窗口的第一个元素且很大），结束窗口
            if projected_window_duration >= 80:
                window_end = app_end_time
                j += 1
                break
            
            j += 1
        else:
            # 所有的应用都处理完了，正常结束
            window_end = final_boundary
        
        # 格式化时间范围
        start_str = window_start.strftime('%H:%M')
        end_str = window_end.strftime('%H:%M')
        time_range = f"{start_str}-{end_str}"
        
        # 添加窗口数据
        if window_apps:
            formatted_apps = {}
            for app, durations in window_apps.items():
                if len(durations) > 5:
                    total_time = round(sum(durations), 1)
                    max_time = round(max(durations), 1)
                    count = len(durations)
                    formatted_apps[app] = f"{count}次(共{total_time}m,最长{max_time}m)"
                else:
                    formatted_apps[app] = durations
            time_range_agg[time_range] = formatted_apps
        
        # i 更新为下一个未处理的应用索引
        i = j
    
    return time_range_agg


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
    
    # 计算按时间范围聚合的应用数据
    phone_app_by_time_range = _compute_app_by_time_range(phone_app_usage, end_datetime)
    computer_app_by_time_range = _compute_app_by_time_range(computer_app_usage, end_datetime)
    
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
        if phone_app_by_time_range:
            aggregated['phone_app_by_time_range'] = phone_app_by_time_range
        
    if computer_summary:
        aggregated['computer_app_summary'] = computer_summary
        if computer_app_by_time_range:
            aggregated['computer_app_by_time_range'] = computer_app_by_time_range
        
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


