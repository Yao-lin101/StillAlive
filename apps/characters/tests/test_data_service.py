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
