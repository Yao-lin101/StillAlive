from django.test import TestCase
from django.utils import timezone
from datetime import timedelta, datetime
from apps.characters.models import Character, CharacterStatus
from apps.users.models import User
from apps.characters.services.data_service import aggregate_status_data

class DataServiceTests(TestCase):
    def setUp(self):
        # Create a test user
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # Create a test character
        self.character = Character.objects.create(
            user=self.user,
            name='Test Character'
        )
        
        self.field_mappings = {
            'phone_app': 'phone',
            'steps': 'steps'
        }

    def test_aggregate_status_data_option_b(self):
        """
        Verify Option B: global_active_time_ranges should only contain:
        1. Yesterday's last active range (e.g. night segment)
        2. Today's active ranges
        It should exclude day-before-yesterday and yesterday's non-last active ranges.
        """
        # Target date: 2024-01-02
        target_date = datetime(2024, 1, 2).date()
        
        # Helper to create status at a specific time with phone usage (so it counts as active)
        def create_status(dt):
            status = CharacterStatus.objects.create(
                character=self.character,
                status_type='status',
                data={'phone': 'WeChat', 'steps': 100}
            )
            # Override timestamp since auto_now_add is set
            CharacterStatus.objects.filter(id=status.id).update(timestamp=timezone.make_aware(dt))

        # 1. Day before yesterday: 2023-12-31 (should be completely ignored)
        create_status(datetime(2023, 12, 31, 15, 0))
        create_status(datetime(2023, 12, 31, 15, 30))

        # 2. Yesterday: 2024-01-01
        # Yesterday Segment 1 (morning, should be ignored): 09:00 - 10:00
        create_status(datetime(2024, 1, 1, 9, 0))
        create_status(datetime(2024, 1, 1, 10, 0))
        
        # Yesterday Segment 2 (night, last segment, should be kept): 22:00 - 23:30
        create_status(datetime(2024, 1, 1, 22, 0))
        create_status(datetime(2024, 1, 1, 23, 30))

        # 3. Today: 2024-01-02
        # Today Segment 1: 09:00 - 11:00
        create_status(datetime(2024, 1, 2, 9, 0))
        create_status(datetime(2024, 1, 2, 11, 0))
        
        # Today Segment 2: 15:00 - 16:00
        create_status(datetime(2024, 1, 2, 15, 0))
        create_status(datetime(2024, 1, 2, 16, 0))

        # End datetime is 2024-01-03 00:00
        end_datetime = timezone.make_aware(datetime(2024, 1, 3, 0, 0))

        # Aggregate data
        aggregated = aggregate_status_data(
            character=self.character,
            field_mappings=self.field_mappings,
            target_date=target_date,
            end_datetime=end_datetime
        )

        self.assertIsNotNone(aggregated)
        
        # Verify day_before_yesterday_active_time_ranges is empty
        self.assertEqual(aggregated.get('day_before_yesterday_active_time_ranges'), [])
        
        # Verify yesterday_active_time_ranges still contains all yesterday ranges for metadata compatibility
        yesterday_ranges = aggregated.get('yesterday_active_time_ranges')
        self.assertEqual(len(yesterday_ranges), 2)
        self.assertIn('09:00-10:00', yesterday_ranges)
        self.assertIn('22:00-23:30', yesterday_ranges)

        # Verify global_active_time_ranges only has yesterday's last range merged with today's ranges
        global_ranges = aggregated.get('global_active_time_ranges')
        print(f"DEBUG: global_ranges = {global_ranges}")
        
        # We expect exactly three ranges:
        # 1. Yesterday 22:00 to Yesterday 23:30
        # 2. 今天 09:00 到 今天 11:00
        # 3. 今天 15:00 到 今天 16:00
        self.assertEqual(len(global_ranges), 3)
        self.assertIn('昨天 22:00 到 昨天 23:30', global_ranges)
        self.assertIn('今天 09:00 到 今天 11:00', global_ranges)
        self.assertIn('今天 15:00 到 今天 16:00', global_ranges)
        
        # Ensure yesterday's morning range (09:00 到 10:00) is NOT in global ranges
        for r in global_ranges:
            self.assertNotIn('09:00', r if '昨天' in r else '')

    def test_aggregate_status_data_app_duration(self):
        """
        Verify that aggregate_status_data correctly computes duration summaries for apps,
        properly cleans app names, and handles time calculations.
        """
        target_date = datetime(2024, 1, 2).date()
        
        def create_status(dt, app):
            status = CharacterStatus.objects.create(
                character=self.character,
                status_type='status',
                data={'phone': app, 'steps': 0}
            )
            CharacterStatus.objects.filter(id=status.id).update(timestamp=timezone.make_aware(dt))

        # We create a sequence of statuses:
        # 1. 2024-01-02 09:00: WeChat: chat with Alice
        # 2. 2024-01-02 09:10: WeChat: chat with Bob (should merge with Alice into WeChat, total duration 10 mins)
        # 3. 2024-01-02 09:15: Chrome: Google Search (duration 15 mins)
        # 4. 2024-01-02 09:30: WeChat: Chat (duration 30 mins, since next hour end)
        create_status(datetime(2024, 1, 2, 9, 0), 'WeChat: chat with Alice')
        create_status(datetime(2024, 1, 2, 9, 10), 'WeChat: chat with Bob')
        create_status(datetime(2024, 1, 2, 9, 15), 'Chrome: Google Search')
        create_status(datetime(2024, 1, 2, 9, 30), 'WeChat: Chat')

        end_datetime = timezone.make_aware(datetime(2024, 1, 3, 0, 0))

        aggregated = aggregate_status_data(
            character=self.character,
            field_mappings=self.field_mappings,
            target_date=target_date,
            end_datetime=end_datetime
        )

        self.assertIsNotNone(aggregated)
        
        # Verify count summary
        phone_summary = aggregated.get('phone_app_summary')
        self.assertIsNotNone(phone_summary)
        # Raw counts (without merging consecutive cleaned apps, or after merging? Wait, _compute_app_summary cleans it and computes counts)
        # Let's check:
        # WeChat: chat with Alice (1)
        # WeChat: chat with Bob (1)
        # Chrome: Google Search (1)
        # WeChat: Chat (1)
        # Cleaned keys: WeChat (3), Chrome (1)
        self.assertEqual(phone_summary.get('WeChat'), 3)
        self.assertEqual(phone_summary.get('Chrome'), 1)

        # Verify duration summary
        phone_duration_summary = aggregated.get('phone_app_duration_summary')
        self.assertIsNotNone(phone_duration_summary)
        
        # Chronological sequence of cleaned apps:
        # - 09:00 to 09:15: WeChat (15 mins) -> Wait! 09:00 WeChat, 09:10 WeChat.
        # Since both clean to WeChat, they are merged.
        # The next different app starts at 09:15 (Chrome).
        # So duration of WeChat is 09:15 - 09:00 = 15 mins.
        # - 09:15 to 09:30: Chrome (15 mins) -> Next app is WeChat at 09:30.
        # So duration of Chrome is 09:30 - 09:15 = 15 mins.
        # - 09:30 to 24:00 (end of day): WeChat (870 mins) -> filtered out as idle (>= 180 mins).
        # Total durations:
        # - WeChat: 15.0 mins.
        # - Chrome: 15.0 mins.
        self.assertAlmostEqual(phone_duration_summary.get('WeChat'), 15.0, places=1)
        self.assertAlmostEqual(phone_duration_summary.get('Chrome'), 15.0, places=1)

        # Verify total active duration
        total_active_duration = aggregated.get('total_active_duration')
        self.assertIsNotNone(total_active_duration)
        self.assertTrue(total_active_duration > 0)

    def test_aggregate_status_data_cross_device_truncation(self):
        """
        Verify that aggregate_status_data truncates app durations based on activity on other devices
        matching the exact logic from the time range calculation (truncates at first other event
        if there are at least 3 other-device events).
        """
        target_date = datetime(2024, 1, 3).date()
        
        def create_status(dt, device, app):
            status = CharacterStatus.objects.create(
                character=self.character,
                status_type='status',
                data={device: app, 'steps': 0}
            )
            CharacterStatus.objects.filter(id=status.id).update(timestamp=timezone.make_aware(dt))
            
        # 1. WeChat segment: Phone WeChat at 09:00. Next phone event is QQ at 10:00.
        # Interrupted by 3 computer events at 09:10, 09:15, 09:20.
        # Truncates at first: 09:10 (duration 10 mins).
        create_status(datetime(2024, 1, 3, 9, 0), 'phone', 'WeChat')
        create_status(datetime(2024, 1, 3, 9, 10), 'computer', 'VS Code')
        create_status(datetime(2024, 1, 3, 9, 15), 'computer', 'VS Code')
        create_status(datetime(2024, 1, 3, 9, 20), 'computer', 'VS Code')
        
        # 2. QQ segment: Phone QQ at 10:00. Next phone event is WeChat at 15:00.
        # Interrupted by 3 computer events at 10:20, 10:25, 10:30.
        # Truncates at first: 10:20 (duration 20 mins).
        create_status(datetime(2024, 1, 3, 10, 0), 'phone', 'QQ')
        create_status(datetime(2024, 1, 3, 10, 20), 'computer', 'VS Code')
        create_status(datetime(2024, 1, 3, 10, 25), 'computer', 'VS Code')
        create_status(datetime(2024, 1, 3, 10, 30), 'computer', 'VS Code')
        
        # 3. Second WeChat segment: Phone WeChat at 15:00.
        # Interrupted by 3 computer events at 15:30, 15:35, 15:40.
        # Truncates at first: 15:30 (duration 30 mins).
        create_status(datetime(2024, 1, 3, 15, 0), 'phone', 'WeChat')
        create_status(datetime(2024, 1, 3, 15, 30), 'computer', 'VS Code')
        create_status(datetime(2024, 1, 3, 15, 35), 'computer', 'VS Code')
        create_status(datetime(2024, 1, 3, 15, 40), 'computer', 'VS Code')

        end_datetime = timezone.make_aware(datetime(2024, 1, 4, 0, 0))
        
        field_mappings = {
            'phone_app': 'phone',
            'computer_app': 'computer',
            'steps': 'steps'
        }
        
        aggregated = aggregate_status_data(
            character=self.character,
            field_mappings=field_mappings,
            target_date=target_date,
            end_datetime=end_datetime
        )
        
        self.assertIsNotNone(aggregated)
        
        # Verify Phone WeChat total = 10m + 30m = 40.0m
        # Verify Phone QQ total = 20.0m
        phone_durations = aggregated.get('phone_app_duration_summary', {})
        self.assertAlmostEqual(phone_durations.get('WeChat'), 40.0, places=1)
        self.assertAlmostEqual(phone_durations.get('QQ'), 20.0, places=1)
