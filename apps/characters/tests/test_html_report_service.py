from django.test import TestCase
from apps.characters.services.html_report_service import (
    _parse_durations_from_time_range,
    _build_app_usage_chart,
    _parse_active_duration,
    build_report_data
)

class HtmlReportServiceTests(TestCase):
    def test_parse_durations_from_time_range(self):
        """
        Verify that _parse_durations_from_time_range extracts durations correctly
        from various raw structures.
        """
        by_time_range = {
            "08:00-09:00": {
                "Chrome: Google": [5.2, 10.4],
                "WeChat: Chatting": "8次(共25.3m,最长5.1m)",
                "System: Idle": 15.0
            }
        }
        durations = _parse_durations_from_time_range(by_time_range)
        
        # Keys should be cleaned to base names
        self.assertIn("Chrome", durations)
        self.assertIn("WeChat", durations)
        self.assertIn("System", durations)
        
        self.assertAlmostEqual(durations["Chrome"], 15.6, places=1)
        self.assertAlmostEqual(durations["WeChat"], 25.3, places=1)
        self.assertAlmostEqual(durations["System"], 15.0, places=1)

    def test_build_app_usage_chart_with_saved_durations(self):
        """
        Verify that _build_app_usage_chart correctly incorporates phone and computer
        app durations when they are already present in raw_data.
        """
        raw_data = {
            "phone_app_summary": {
                "WeChat": 5,
                "Safari": 2
            },
            "phone_app_duration_summary": {
                "WeChat": 35.5,
                "Safari": 10.0
            },
            "computer_app_summary": {
                "VS Code": 10
            },
            "computer_app_duration_summary": {
                "VS Code": 120.0
            },
            "total_active_duration": 140.0
        }
        
        chart_data = _build_app_usage_chart(raw_data)
        
        self.assertEqual(chart_data["total_phone_records"], 7)
        self.assertEqual(chart_data["total_computer_records"], 10)
        self.assertAlmostEqual(chart_data["total_phone_duration"], 45.5, places=1)
        self.assertAlmostEqual(chart_data["total_computer_duration"], 120.0, places=1)
        self.assertAlmostEqual(chart_data["total_active_duration"], 140.0, places=1)
        
        # Verify app lists contain duration
        phone_list = chart_data["phone"]
        self.assertEqual(len(phone_list), 2)
        self.assertEqual(phone_list[0]["name"], "WeChat")
        self.assertEqual(phone_list[0]["count"], 5)
        self.assertAlmostEqual(phone_list[0]["duration"], 35.5, places=1)
        
        self.assertEqual(phone_list[1]["name"], "Safari")
        self.assertEqual(phone_list[1]["count"], 2)
        self.assertAlmostEqual(phone_list[1]["duration"], 10.0, places=1)

    def test_build_app_usage_chart_fallback(self):
        """
        Verify that _build_app_usage_chart falls back to parsing by_time_range
        when duration summary is not saved in raw_data (i.e. legacy report compatibility).
        """
        raw_data = {
            "phone_app_summary": {
                "WeChat": 5
            },
            "phone_app_by_time_range": {
                "09:00-10:00": {
                    "WeChat": "5次(共40.0m,最长12.0m)"
                }
            }
        }
        
        chart_data = _build_app_usage_chart(raw_data)
        
        self.assertAlmostEqual(chart_data["total_phone_duration"], 40.0, places=1)
        phone_list = chart_data["phone"]
        self.assertEqual(len(phone_list), 1)
        self.assertEqual(phone_list[0]["name"], "WeChat")
        self.assertAlmostEqual(phone_list[0]["duration"], 40.0, places=1)

    def test_parse_active_duration(self):
        """
        Verify that _parse_active_duration extracts the active duration correctly
        both from total_active_duration and active_time_ranges fallback.
        """
        # Test Case 1: Value is present directly
        raw_data_direct = {
            "total_active_duration": 45.2
        }
        self.assertAlmostEqual(_parse_active_duration(raw_data_direct), 45.2, places=1)
        
        # Test Case 2: Value is not present, fall back to parsing active_time_ranges
        raw_data_fallback = {
            "active_time_ranges": [
                "09:00-11:15", # 135 mins
                "15:30-16:00", # 30 mins
                "23:30-00:30"  # 60 mins (cross-day)
            ]
        }
        self.assertAlmostEqual(_parse_active_duration(raw_data_fallback), 225.0, places=1)

    def test_format_app_usage_with_durations(self):
        """
        Verify that format_app_usage combines both counts and durations in prompt outputs.
        """
        from apps.characters.services.llm_utils import format_app_usage
        
        data_summary = {
            "phone_app_summary": {
                "WeChat": 10,
                "Safari": 3
            },
            "phone_app_duration_summary": {
                "WeChat": 45.2,
                "Safari": 5.0
            }
        }
        
        formatted = format_app_usage(data_summary, 'phone')
        
        # WeChat should have both count and duration
        self.assertIn('"WeChat": "10次 (共45.2m)"', formatted)
        self.assertIn('"Safari": "3次 (共5.0m)"', formatted)

    def test_format_app_usage_legacy_fallback(self):
        """
        Verify that format_app_usage correctly parses durations from time range for legacy reports.
        """
        from apps.characters.services.llm_utils import format_app_usage
        
        data_summary = {
            "phone_app_summary": {
                "WeChat": 5
            },
            "phone_app_by_time_range": {
                "09:00-10:00": {
                    "WeChat": "5次(共40.0m,最长12.0m)"
                }
            }
        }
        
        formatted = format_app_usage(data_summary, 'phone')
        self.assertIn('"WeChat": "5次 (共40.0m)"', formatted)
