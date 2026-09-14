import contextlib
import io
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import data_sources as s
import equity_schedules as e
import update_calendar as c
from test_calendar import NOW, fixture_rows

# Minimal factual fixtures transcribed from the issuer PDFs linked in SOURCE_DESIGN.md.
VOO = '''S&P 500 ETF 922908363 VOO 03/27/26 03/27/26 03/31/26
06/26/26 06/26/26 06/30/26
09/28/26 09/28/26 09/30/26
12/21/26 12/21/26 12/23/26
12/30/26 12/30/26 01/04/27
Next fund'''
QQQ = '''Invesco QQQ TrustSM
Third quarter distribution calendar year 2026
Ex-dividend date Declaration date Record date Payment date
18 September 2026 20 September 2026 21 September 2026 08 October 2026'''


class EquityScheduleTests(unittest.TestCase):
    def test_vanguard_selects_exact_fund_and_excludes_supplemental_slot(self):
        rows = e.parse_vanguard(VOO, 2026)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[2]['payable_date'], '2026-09-30')
        for bad in (VOO.replace('VOO ', 'VOOG '), VOO.replace('09/28/26 09/28/26 09/30/26', ''),
                    VOO.replace('06/26/26', '03/27/26')):
            with self.assertRaises(s.SourceError):
                e.parse_vanguard(bad, 2026)

    def test_qqq_date_only_announcement_needs_no_amount(self):
        self.assertEqual(e.parse_qqq(QQQ, 2026),
                         [{'cycle': [2026, 3], 'payable_date': '2026-10-08'}])
        for bad in (QQQ.replace('QQQ', 'QQQM'), QQQ.replace('2026', '2025'),
                    QQQ.replace('08 October 2026', '08 October 2027')):
            with self.assertRaises(s.SourceError):
                e.parse_qqq(bad, 2026)

    def test_qqq_followup_us_dtc_date(self):
        text = ('Invesco QQQ TrustSM Third quarter distribution calendar year 2026. '
                'The payment date is 08 October 2026 when the Depository Trust Company pays.')
        self.assertEqual(e.parse_qqq(text, 2026)[0]['payable_date'], '2026-10-08')

    def test_directory_newest_revision_only_and_no_external_links(self):
        base = '/listedco/listconews/sehk/2026/0904/'
        html = ''.join(f'<a href="{url}">{title}</a>' for url, title in [
            ('https://evil.example/'+base+'1.pdf', 'Third Quarter Distribution Announcement'),
            (base+'2.pdf', 'Third Quarter Distribution Announcement (form)'),
            (base+'3.pdf', 'Third Quarter Distribution Announcement'),
            (base+'4.pdf', 'Third Quarter Distribution Announcement'),
            (base+'5.pdf', 'Second Quarter Distribution Announcement')])
        self.assertEqual(e.qqq_links(html.encode(), 2026, 3), ['https://www1.hkexnews.hk'+base+'3.pdf'])

    def test_cached_official_date_expires_without_refreshing_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            source = s.DividendSources(Path(tmp), NOW)
            with patch.object(e, 'fetch_dates', return_value=e.parse_qqq(QQQ, 2026)):
                self.assertEqual(source.equity_schedule('QQQ')[(2026, 3)]['date_status'], '官方支付日')
            with patch.object(e, 'fetch_dates', side_effect=s.SourceError('offline')):
                source.now = NOW + timedelta(days=6)
                self.assertEqual(source.equity_schedule('QQQ')[(2026, 3)]['date_status'], '官方支付日（缓存）')
                self.assertEqual(source.cache['QQQ-schedule-2026']['fetched_at'], NOW.isoformat())
                source.now = NOW + timedelta(days=8)
                self.assertEqual(source.equity_schedule('QQQ'), {})

    def test_official_dates_replace_old_dates_and_keep_amounts_estimated(self):
        positions = {t: {'shares': 100, 'source': 'stockanalysis', 'frequency': 4} for t in ('QQQ', 'VOO')}
        rows = {t: [{**r, 'ticker': t} for r in fixture_rows('QQQ')] for t in positions}
        for row in rows['VOO']:
            if row['ex_date'].year == 2025 and row['ex_date'].month == 9:
                row['payable_date'] = date(2025, 10, 1)
        official = {t: {(2026, 3): {'payable_date': d, 'date_status': '官方支付日'}}
                    for t, d in [('QQQ', date(2026, 10, 8)), ('VOO', date(2026, 9, 30))]}
        start, end = date(2026, 9, 1), date(2027, 9, 1)
        sched = c.make_schedule(positions, rows, [], '', start, end, official)
        metrics = {t: {'event_amount': Decimal(100)} for t in positions}
        text = '\n'.join(c.unfold(c.build_calendar({}, positions, metrics, sched, start, start, end, NOW)))
        self.assertNotIn('DTSTART;VALUE=DATE:20261001', text)
        self.assertNotIn('DTSTART;VALUE=DATE:20261030', text)
        for day in ('20260930', '20261008'):
            event = next(v for v in text.split('BEGIN:VEVENT') if 'DTSTART;VALUE=DATE:'+day in v)
            self.assertIn('预扣税10%', event)
            self.assertNotIn('预计本日发放', event)
            self.assertIn('｜100.0股｜$90.0', event)
        self.assertIn('预计本日发放', text)  # future unpublished quarters remain projected

        # An actual cash announcement later wins even if it moves the official date.
        actual = {date(2026, 10, 9): [{'ticker': 'QQQ', 'ex_date': date(2026, 9, 21),
                   'payable_date': date(2026, 10, 9), 'amount': Decimal(120)}]}
        changed = c.build_calendar(actual, positions, metrics, sched, start, start, end, NOW)
        self.assertNotIn('DTSTART;VALUE=DATE:20261008', changed)
        self.assertIn('DTSTART;VALUE=DATE:20261009', changed)

    def test_generate_calendar_fetches_official_equity_dates(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            source = s.DividendSources(Path(tmp), NOW)
            rows = [{**r, 'provider': 'Nasdaq', 'cached': False} for r in fixture_rows('QQQ')]
            with patch.object(source, 'fetch', return_value=rows), \
                 patch.object(e, 'fetch_dates', return_value=e.parse_qqq(QQQ, 2026)) as fetch:
                text = c.generate_calendar({'positions': {'QQQ': {'shares': 100, 'source': 'stockanalysis', 'frequency': 4}}}, source)
            fetch.assert_called_once_with('QQQ', 2026, 3)
            self.assertIn('DTSTART;VALUE=DATE:20261008', text)
            self.assertNotIn('DTSTART;VALUE=DATE:20261030', text)
