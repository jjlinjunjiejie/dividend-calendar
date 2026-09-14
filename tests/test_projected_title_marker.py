import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

import update_calendar as c


class ProjectedTitleMarkerTests(unittest.TestCase):
    def render_scheduled(self, status: str) -> str:
        day = date(2026, 10, 30)
        positions = {'QQQ': {'shares': 100, 'source': 'stockanalysis', 'frequency': 4}}
        metrics = {'QQQ': {'event_amount': Decimal('100')}}
        scheduled = {day: {'QQQ': {'cycle': (2026, 4), 'date_status': status}}}
        return c.build_calendar({}, positions, metrics, scheduled,
                                date(2026, 9, 1), date(2026, 9, 1), date(2027, 1, 1),
                                datetime(2026, 9, 14, tzinfo=timezone.utc))

    def test_projected_payment_date_prefixes_title_with_diamond(self):
        text = '\n'.join(c.unfold(self.render_scheduled('推算支付日')))
        self.assertIn('SUMMARY:◇90', text)
        self.assertIn('预扣税10%｜预计本日发放', text)

    def test_official_payment_date_keeps_plain_numeric_title(self):
        text = '\n'.join(c.unfold(self.render_scheduled('官方支付日')))
        self.assertIn('SUMMARY:90', text)
        self.assertNotIn('SUMMARY:◇', text)
        self.assertNotIn('预计本日发放', text)

    def test_actual_distribution_keeps_plain_numeric_title(self):
        day = date(2026, 10, 8)
        positions = {'QQQ': {'shares': 100, 'source': 'stockanalysis', 'frequency': 4}}
        metrics = {'QQQ': {'event_amount': Decimal('100')}}
        actual = {day: [{'ticker': 'QQQ', 'ex_date': date(2026, 9, 21),
                         'payable_date': day, 'amount': Decimal('100')}]} 
        text = '\n'.join(c.unfold(c.build_calendar(
            actual, positions, metrics, {}, date(2026, 9, 1), date(2026, 9, 1), date(2027, 1, 1),
            datetime(2026, 9, 14, tzinfo=timezone.utc))))
        self.assertIn('SUMMARY:90', text)
        self.assertNotIn('SUMMARY:◇', text)


if __name__ == '__main__':
    unittest.main()
