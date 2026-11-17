from datetime import date, datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.characters.models import Character, DailyReport, DailyReportConfig
from apps.users.models import User


@override_settings(TIME_ZONE='Asia/Shanghai')
class DailyReportVisibilityTests(TestCase):
    today = date(2026, 5, 2)

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email='report-owner@example.test', password='test')
        cls.visitor = User.objects.create_user(email='report-visitor@example.test', password='test')
        cls.character = Character.objects.create(
            user=cls.owner, name='Report character', display_code='rpt001',
        )
        cls.config = DailyReportConfig.objects.create(
            character=cls.character, is_enabled=True, visibility='public',
        )
        cls.reports = {}
        for days_ago in (-1, 0, 1, 2, 3, 6, 7, 40):
            cls.reports[days_ago] = DailyReport.objects.create(
                character=cls.character,
                date=cls.today - timedelta(days=days_ago),
                is_hidden=days_ago == 1,
                analysis_result={'markdown': f'Report {days_ago}'},
            )

    def setUp(self):
        self.client = APIClient()
        # 上海已跨到 5 月 2 日，UTC 仍为 5 月 1 日，验证按本地日期计算。
        clock = patch(
            'django.utils.timezone.now',
            return_value=datetime(2026, 5, 1, 16, 30, tzinfo=datetime_timezone.utc),
        )
        clock.start()
        self.addCleanup(clock.stop)
        self.config_url = reverse(
            'character-daily-report-list', kwargs={'character_pk': self.character.uid},
        )
        self.dates_url = reverse('daily-report-dates', kwargs={'code': self.character.display_code})
        self.detail_url = reverse('daily-report-detail', kwargs={'code': self.character.display_code})

    def assert_visible_reports(self, allowed_days):
        months = {(report.date.year, report.date.month) for report in self.reports.values()}
        for year, month in months:
            response = self.client.get(self.dates_url, {'year': year, 'month': month})
            self.assertEqual(response.status_code, 200)
            expected_dates = [
                self.reports[days_ago].date.day for days_ago in allowed_days
                if (self.reports[days_ago].date.year, self.reports[days_ago].date.month) == (year, month)
            ]
            self.assertCountEqual(response.data['dates'], expected_dates)

        for days_ago, report in self.reports.items():
            response = self.client.get(self.detail_url, {'date': report.date.isoformat()})
            expected_status = 200 if days_ago in allowed_days else 404
            self.assertEqual(response.status_code, expected_status, (days_ago, response.data))
            if expected_status == 200:
                self.assertEqual(response.data['markdown'], f'Report {days_ago}')

    def test_public_ranges_apply_to_anonymous_and_authenticated_visitors(self):
        for user in (None, self.visitor):
            self.client.force_authenticate(user=user)
            for visible_days, allowed_days in (
                (0, [-1, 0, 2, 3, 6, 7, 40]),
                (3, [0, 2]),
                (7, [0, 2, 3, 6]),
            ):
                with self.subTest(user=user, visible_days=visible_days):
                    self.config.public_visible_days = visible_days
                    self.config.save()
                    self.assert_visible_reports(allowed_days)

    def test_owner_can_view_all_reports_including_old_and_hidden_reports(self):
        self.client.force_authenticate(user=self.owner)
        for visible_days in (0, 3, 7):
            with self.subTest(visible_days=visible_days):
                self.config.public_visible_days = visible_days
                self.config.save()
                self.assert_visible_reports(list(self.reports))

    def test_private_reports_remain_visible_only_to_owner(self):
        self.config.visibility = 'private'
        self.config.public_visible_days = 3
        self.config.save()
        for user in (None, self.visitor, self.owner):
            with self.subTest(user=user):
                self.client.force_authenticate(user=user)
                self.assert_visible_reports(list(self.reports) if user == self.owner else [])

    def test_disabled_or_missing_config_prevents_report_access(self):
        self.config.is_enabled = False
        self.config.save()
        for user in (None, self.owner):
            self.client.force_authenticate(user=user)
            self.assert_visible_reports([])

        self.config.delete()
        self.client.force_authenticate(user=None)
        self.assert_visible_reports([])

    def test_public_window_moves_forward_when_local_date_changes(self):
        self.config.public_visible_days = 3
        self.config.save()
        report = self.reports[2]
        response = self.client.get(self.detail_url, {'date': report.date.isoformat()})
        self.assertEqual(response.status_code, 200)

        with patch(
            'django.utils.timezone.now',
            return_value=datetime(2026, 5, 2, 16, 30, tzinfo=datetime_timezone.utc),
        ):
            response = self.client.get(self.detail_url, {'date': report.date.isoformat()})
            self.assertEqual(response.status_code, 404)
            response = self.client.get(self.dates_url, {'year': report.date.year, 'month': report.date.month})
            self.assertEqual(response.data['dates'], [])

    def test_existing_and_missing_configs_default_to_all_dates(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.get(self.config_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['public_visible_days'], 0)

        self.config.delete()
        response = self.client.get(self.config_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['public_visible_days'], 0)
        response = self.client.post(self.config_url, {'visibility': 'public'}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['public_visible_days'], 0)
        self.assertEqual(DailyReportConfig.objects.get(character=self.character).public_visible_days, 0)

    def test_settings_accept_only_supported_ranges_and_preserve_partial_updates(self):
        self.client.force_authenticate(user=self.owner)
        for visible_days in (3, 7, 0):
            response = self.client.post(self.config_url, {'public_visible_days': visible_days}, format='json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data['public_visible_days'], visible_days)
            self.config.refresh_from_db()
            self.assertEqual(self.config.public_visible_days, visible_days)

        self.client.post(self.config_url, {'public_visible_days': 7}, format='json')
        response = self.client.post(self.config_url, {'visibility': 'private'}, format='json')
        self.assertEqual(response.data['public_visible_days'], 7)
        response = self.client.post(self.config_url, {'visibility': 'public'}, format='json')
        self.assertEqual(response.data['public_visible_days'], 7)

        for invalid_days in (-1, 1, 30, None, 'invalid'):
            with self.subTest(invalid_days=invalid_days):
                response = self.client.post(
                    self.config_url, {'public_visible_days': invalid_days}, format='json',
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn('public_visible_days', response.data)
                self.config.refresh_from_db()
                self.assertEqual(self.config.public_visible_days, 7)

    def test_another_user_cannot_update_public_range(self):
        self.client.force_authenticate(user=self.visitor)
        response = self.client.post(self.config_url, {'public_visible_days': 3}, format='json')
        self.assertEqual(response.status_code, 404)
        self.config.refresh_from_db()
        self.assertEqual(self.config.public_visible_days, 0)
