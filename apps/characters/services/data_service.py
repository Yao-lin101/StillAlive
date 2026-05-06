import logging
from django.utils import timezone
from datetime import timedelta, datetime
from collections import defaultdict, Counter
from apps.characters.models import CharacterStatus

logger = logging.getLogger(__name__)

# 活跃时间区间的最大间隔（分钟）
ACTIVE_INTERVAL_MAX_GAP = 60

def _extract_raw_usage(statuses, field_mappings):
    """提取手机应用、电脑应用和步数的原始流水，以及当天的活跃小时集合和活跃时间点"""
    phone_key = field_mappings.get('phone_app')
    computer_key = field_mappings.get('computer_app')
    steps_key = field_mappings.get('steps')
    
    phone_app_usage = []
    computer_app_usage = []
    steps_data = []
    active_hours = set()
    active_timestamps = []
    
    for status in statuses:
        local_timestamp = timezone.localtime(status.timestamp)
        data = status.data
        hour = local_timestamp.hour
        
        # 只在存在映射字段的数据时才记录活跃时间
        has_relevant_data = False
        
        if phone_key and phone_key in data:
            value = data[phone_key]
            if value:
                phone_app_usage.append({
                    'hour': hour,
                    'app': str(value),
                    'timestamp': local_timestamp
                })
                has_relevant_data = True
        
        if computer_key and computer_key in data:
            value = data[computer_key]
            if value:
                computer_app_usage.append({
                    'hour': hour,
                    'app': str(value),
                    'timestamp': local_timestamp
                })
                has_relevant_data = True
        
        if steps_key and steps_key in data:
            try:
                steps_data.append({
                    'hour': hour,
                    'steps': int(data[steps_key])
                })
                has_relevant_data = True
            except (ValueError, TypeError):
                pass
        
        # 只有当存在相关数据时才记录活跃时间
        if has_relevant_data:
            active_hours.add(hour)
            active_timestamps.append(local_timestamp)
                
    return phone_app_usage, computer_app_usage, steps_data, sorted(list(active_hours)), sorted(active_timestamps)


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


def _get_historical_active_hours(character, start_time, end_time, field_mappings):
    """查询指定时间段内的活跃小时集合和活跃时间点"""
    phone_key = field_mappings.get('phone_app')
    computer_key = field_mappings.get('computer_app')
    steps_key = field_mappings.get('steps')
    
    statuses = CharacterStatus.objects.filter(
        character=character,
        timestamp__gte=start_time,
        timestamp__lt=end_time
    ).order_by('timestamp')
    
    active_hours = set()
    active_timestamps = []
    for status in statuses:
        local_ts = timezone.localtime(status.timestamp)
        data = status.data
        
        # 只在存在映射字段的数据时才记录活跃时间
        has_relevant_data = False
        
        if phone_key and phone_key in data and data[phone_key]:
            has_relevant_data = True
        if computer_key and computer_key in data and data[computer_key]:
            has_relevant_data = True
        if steps_key and steps_key in data:
            try:
                int(data[steps_key])
                has_relevant_data = True
            except (ValueError, TypeError):
                pass
        
        # 只有当存在相关数据时才记录活跃时间
        if has_relevant_data:
            active_hours.add(local_ts.hour)
            active_timestamps.append(local_ts)
    return sorted(list(active_hours)), active_timestamps


def _compute_active_time_ranges(active_timestamps, last_record_time):
    """
    计算活跃时间区间，剔除超过ACTIVE_INTERVAL_MAX_GAP分钟的间隔
    
    Args:
        active_timestamps: 活跃时间点列表，已排序
        last_record_time: 最后一次同步的时间
        
    Returns:
        list: 活跃时间区间列表，格式为 [(start_time, end_time), ...]
    """
    if not active_timestamps:
        return []
    
    time_ranges = []
    current_start = active_timestamps[0]
    current_end = active_timestamps[0]
    
    for i in range(1, len(active_timestamps)):
        current_time = active_timestamps[i]
        time_diff = (current_time - current_end).total_seconds() / 60
        
        # 如果时间间隔超过最大间隔，结束当前区间并开始新区间
        if time_diff > ACTIVE_INTERVAL_MAX_GAP:
            time_ranges.append((current_start, current_end))
            current_start = current_time
        
        current_end = current_time
    
    # 添加最后一个区间，结束时间为最后一次同步的时间
    if current_start:
        # 如果距离最后一次活动的时间仍在允许的间隔内，则延伸到最后一次同步时间
        if last_record_time and (last_record_time - current_end).total_seconds() / 60 <= ACTIVE_INTERVAL_MAX_GAP:
            final_end = last_record_time
        else:
            final_end = current_end
        time_ranges.append((current_start, final_end))
    
    return time_ranges


def _compute_qq_messages_summary(qq_messages):
    """
    计算QQ消息的汇总统计
    
    Args:
        qq_messages: QQ消息记录列表
    
    Returns:
        dict: QQ消息统计摘要
    """
    if not qq_messages:
        return None
    
    total_group_message_blocks = 0
    total_private_message_blocks = 0
    total_user_messages = 0
    group_message_count_by_group = defaultdict(int)
    message_timestamps = []
    
    for msg_record in qq_messages:
        # 处理群消息
        if msg_record.message_type == 'group':
            message_blocks = msg_record.message_data
            total_group_message_blocks += len(message_blocks)
            
            for block in message_blocks:
                # 统计每个群的消息块数量
                group_name = block.get('群名称', '未知群聊')
                group_message_count_by_group[group_name] += 1
                
                # 检查是否有用户消息
                for key, value in block.items():
                    if key != '时间' and key != '群名称' and '用户' in key:
                        total_user_messages += 1
                
                # 记录消息时间戳
                message_timestamps.append(msg_record.timestamp)
        
        # 处理私聊消息
        elif msg_record.message_type == 'private':
            message_blocks = msg_record.message_data
            total_private_message_blocks += len(message_blocks)
            
            for block in message_blocks:
                # 检查是否有用户消息
                if '用户' in block or '话题' in block:
                    total_user_messages += 1
                
            # 记录消息时间戳
            message_timestamps.append(msg_record.timestamp)
    
    # 计算总消息块数
    total_message_blocks = total_group_message_blocks + total_private_message_blocks
    
    # 构建摘要
    summary = {
        'total_message_blocks': total_message_blocks,
        'group_message_blocks_count': total_group_message_blocks,
        'private_message_blocks_count': total_private_message_blocks,
        'user_messages_count': total_user_messages,
        'group_message_count_by_group': dict(group_message_count_by_group)
    }
    
    return summary


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
    
    phone_app_usage, computer_app_usage, steps_data, active_hours, active_timestamps = _extract_raw_usage(statuses, field_mappings)
    
    phone_summary, phone_by_hour = _compute_app_summary(phone_app_usage)
    computer_summary, computer_by_hour = _compute_app_summary(computer_app_usage)
    steps_summary, steps_by_hour = _compute_steps_summary(steps_data)
    
    # 计算按时间范围聚合的应用数据
    phone_app_by_time_range = _compute_app_by_time_range(phone_app_usage, end_datetime, other_usage=computer_app_usage)
    computer_app_by_time_range = _compute_app_by_time_range(computer_app_usage, end_datetime, other_usage=phone_app_usage)
    
    # 计算活跃时间区间
    last_record_time = timezone.localtime(latest_status.timestamp) if latest_status else None
    active_time_ranges = _compute_active_time_ranges(active_timestamps, last_record_time)
    
    # 格式化活跃时间区间为字符串列表
    formatted_active_ranges = []
    for start, end in active_time_ranges:
        start_str = start.strftime('%H:%M')
        end_str = end.strftime('%H:%M')
        formatted_active_ranges.append(f"{start_str}-{end_str}")
    
    aggregated = {
        'date': target_date.isoformat(),
        'total_records': statuses.count(),
        'last_record_time': last_record_time.isoformat() if last_record_time else None,
        'data_cutoff_time': timezone.localtime(end_datetime).isoformat() if timezone.is_aware(end_datetime) else end_datetime.isoformat(),
        'active_time_ranges': formatted_active_ranges,
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
    
    # 处理QQ消息数据
    from apps.characters.models import QQMessage
    qq_messages = QQMessage.objects.filter(
        character=character,
        date=target_date
    ).order_by('timestamp')
    qq_summary = _compute_qq_messages_summary(qq_messages)
    if qq_summary:
        aggregated['qq_messages_summary'] = qq_summary
        # 添加原始QQ消息数据，用于在日报中展示详情
        # 提取可序列化的数据，避免直接添加QQMessage对象
        qq_messages_data = []
        for msg in qq_messages:
            qq_messages_data.append({
                'message_type': msg.message_type,
                'message_data': msg.message_data,
                'timestamp': msg.timestamp.isoformat() if msg.timestamp else None
            })
        aggregated['qq_messages'] = qq_messages_data
        
    # 处理昨天的活跃时间
    yesterday_hours, yesterday_timestamps = _get_historical_active_hours(
        character, 
        start_datetime - timedelta(days=1), 
        start_datetime,
        field_mappings
    )
    # 计算昨天的活跃时间区间
    yesterday_ranges = _compute_active_time_ranges(yesterday_timestamps, None)
    formatted_yesterday_ranges = []
    for start, end in yesterday_ranges:
        start_str = start.strftime('%H:%M')
        end_str = end.strftime('%H:%M')
        formatted_yesterday_ranges.append(f"{start_str}-{end_str}")
    
    # 处理前天的活跃时间
    day_before_yesterday_hours, day_before_yesterday_timestamps = _get_historical_active_hours(
        character, 
        start_datetime - timedelta(days=2), 
        start_datetime - timedelta(days=1),
        field_mappings
    )
    # 计算前天的活跃时间区间
    day_before_yesterday_ranges = _compute_active_time_ranges(day_before_yesterday_timestamps, None)
    formatted_day_before_yesterday_ranges = []
    for start, end in day_before_yesterday_ranges:
        start_str = start.strftime('%H:%M')
        end_str = end.strftime('%H:%M')
        formatted_day_before_yesterday_ranges.append(f"{start_str}-{end_str}")
    
    aggregated['yesterday_active_time_ranges'] = formatted_yesterday_ranges
    aggregated['day_before_yesterday_active_time_ranges'] = formatted_day_before_yesterday_ranges
    
    # 全局时间轴合并（跨越前天、昨天、今天的三天数据统一合并）
    def format_relative_time(dt, t_date):
        local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
        if local_dt.date() == t_date:
            return f"今天 {local_dt.strftime('%H:%M')}"
        elif local_dt.date() == t_date - timedelta(days=1):
            return f"昨天 {local_dt.strftime('%H:%M')}"
        elif local_dt.date() == t_date - timedelta(days=2):
            return f"前天 {local_dt.strftime('%H:%M')}"
        else:
            return local_dt.strftime('%m-%d %H:%M')

    all_timestamps = day_before_yesterday_timestamps + yesterday_timestamps + active_timestamps
    # active_timestamps 已通过各天的查询得到，按顺序拼接即为递增状态
    global_ranges = _compute_active_time_ranges(all_timestamps, last_record_time)
    
    formatted_global_ranges = []
    for start, end in global_ranges:
        start_str = format_relative_time(start, target_date)
        end_str = format_relative_time(end, target_date)
        formatted_global_ranges.append(f"{start_str} 到 {end_str}")
        
    aggregated['global_active_time_ranges'] = formatted_global_ranges
    
    return aggregated


