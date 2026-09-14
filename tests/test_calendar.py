import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import data_sources as s
import run_update as runner
import update_calendar as c

FIX = Path(__file__).parent / 'fixtures'
NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
INFO = {'source': 'ishares', 'portfolio_id': 319422, 'shares': 100, 'maturity_year': 2027}
POS = {'IBHG': INFO}
DEC1, DEC2 = date(2026, 12, 4), date(2026, 12, 23)
NASDAQ_ROWS = s.nasdaq_rows


def fixture_rows(ticker='IBHG'):
    if ticker == 'IBHG':
        with patch.object(s, 'request_json', return_value=json.loads((FIX/'ishares.json').read_text())):
            return s.ishares_rows(ticker, 319422, s.HOSTS[0])
    with patch.object(s, 'request_json', return_value=json.loads((FIX/'nasdaq.json').read_text())):
        return NASDAQ_ROWS(ticker)


def actual(pay, ex=None, amount='100', cached=False):
    return {'ticker': 'IBHG', 'ex_date': ex or pay, 'record_date': ex or pay,
            'payable_date': pay, 'amount': Decimal(amount), 'per_share': 1,
            'provider': 'iShares', 'cached': cached}


def render(rows=None, scheduled=None, now=NOW):
    metrics = {'IBHG': {'event_amount': Decimal(100), 'provider': 'iShares', 'method': '30日SEC收益率'}}
    return c.build_calendar(rows or {}, POS, metrics, scheduled or {}, date(2026, 9, 1),
                            date(2026, 12, 1), date(2027, 2, 1), now)


def schedule(*dates):
    return {d: {'IBHG': {'cycle': c.monthly_cycle(d), 'date_status': '官方支付日'}} for d in dates}


class CalendarTests(unittest.TestCase):
    def test_december_has_two_full_estimates(self):
        text = render(scheduled=schedule(DEC1, DEC2))
        self.assertEqual(text.count('SUMMARY:90\r\n'), 2)

    def test_first_announcement_does_not_erase_second(self):
        text = render({DEC1: [actual(DEC1)]}, schedule(DEC1, DEC2))
        self.assertEqual(text.count('BEGIN:VEVENT'), 2)
        self.assertEqual(text.count('SUMMARY:90\r\n'), 2)
        self.assertIn('DTSTART;VALUE=DATE:20261223', text)

    def test_actual_moved_pay_date_consumes_only_its_cycle(self):
        moved = date(2026, 12, 7)
        text = render({moved: [actual(moved, date(2026,12,1))]}, schedule(DEC1, DEC2))
        self.assertNotIn('DTSTART;VALUE=DATE:20261204', text)
        self.assertIn('DTSTART;VALUE=DATE:20261207', text)
        self.assertIn('DTSTART;VALUE=DATE:20261223', text)

    def test_actual_conditional_january_payment_remains(self):
        jan = date(2027, 1, 5)
        text = render({jan: [actual(jan, date(2026,12,30))]}, schedule(DEC1, DEC2))
        self.assertEqual(text.count('BEGIN:VEVENT'), 3)
        self.assertIn('DTSTART;VALUE=DATE:20270105', text)

    def test_actual_estimate_and_cache_use_compact_memo(self):
        for text in [render({DEC1: [actual(DEC1)]}),
                     render(scheduled=schedule(DEC1)),
                     render({DEC1: [actual(DEC1, cached=True)]})]:
            description = next(line for line in c.unfold(text) if line.startswith('DESCRIPTION:'))
            self.assertEqual(description, 'DESCRIPTION:IBHG｜100.0股｜$90.0')

    def test_round_only_at_final_display(self):
        text = '\n'.join(c.unfold(render({DEC1: [actual(DEC1, amount='111.111')]})))
        self.assertIn('SUMMARY:99', text)
        self.assertIn('｜$99.9', text)

    def test_unchanged_events_are_byte_identical(self):
        old = render({DEC1: [actual(DEC1)]})
        newer = render({DEC1: [actual(DEC1)]}, now=NOW+timedelta(hours=6))
        self.assertEqual(c.stabilize_events(newer, old), old)

    def test_changed_event_increments_sequence_and_preserves_other_event(self):
        old = render({DEC1:[actual(DEC1)], DEC2:[actual(DEC2)]})
        new = render({DEC1:[actual(DEC1, amount='120')], DEC2:[actual(DEC2)]}, now=NOW+timedelta(hours=6))
        result = c.stabilize_events(new, old)
        self.assertEqual(result.count('DTSTAMP:20260914T180000Z'), 1)
        self.assertEqual(result.count('DTSTAMP:20260914T120000Z'), 1)
        self.assertEqual(result.count('SEQUENCE:1'), 1)

    def test_folded_utf8_and_crlf_round_trip(self):
        text = render({DEC1: [actual(DEC1)]})
        self.assertTrue(all(len(line.encode('utf8')) <= 73 for line in text.splitlines()))
        self.assertEqual(c.encode_lines(c.unfold(text)), text)
        self.assertNotIn('\n', text.replace('\r\n', ''))

    def test_monthly_projection_skips_january_and_preserves_two_december_dates(self):
        dates = c.projected_monthly_dates([date(2026,11,5), DEC1, DEC2], date(2026,12,1), date(2027,3,1))
        self.assertEqual(sum(d.month == 12 for d in dates), 2)
        self.assertFalse(any(d.month == 1 for d in dates))
        self.assertEqual(sum(d.month == 2 for d in dates), 1)

    def test_quarterly_actual_replaces_date_projection(self):
        info = {'shares':100, 'source':'stockanalysis', 'frequency':4}
        rows = fixture_rows('QQQ')
        rows = sorted(rows, key=lambda r:r['payable_date'])
        sched = c.make_schedule({'QQQ':info}, {'QQQ':rows}, [], '', date(2026,3,1), date(2027,4,1))
        row = rows[-2]  # March 2026 actual
        grouped = {row['payable_date']:[{**row,'amount':Decimal(100),'provider':'Nasdaq'}]}
        metrics = {'QQQ':{'event_amount':Decimal(100),'provider':'Nasdaq','method':'history'}}
        text = c.build_calendar(grouped, {'QQQ':info}, metrics, sched, date(2026,3,1), date(2026,3,1), date(2027,4,1), NOW)
        self.assertEqual(text.count('DTSTART;VALUE=DATE:20260327'), 1)

    def test_metrics_can_use_history_without_quote_data(self):
        rows = [{**r,'provider':'Nasdaq','cached':True} for r in fixture_rows('QQQ')]
        metric = c.market_metrics({'QQQ':{'source':'stockanalysis','shares':100}}, {'QQQ':rows}, {}, NOW.date())['QQQ']
        self.assertEqual(metric['method'], '近12个月实际分配')
        self.assertTrue(metric['cached'])
        self.assertEqual(metric['event_amount'], Decimal('75.858500'))

    def test_atomic_write_skips_identical_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'calendar.ics'
            self.assertTrue(c.atomic_write_if_changed(path,'same'))
            before = path.stat().st_mtime_ns
            self.assertFalse(c.atomic_write_if_changed(path,'same'))
            self.assertEqual(before,path.stat().st_mtime_ns)

    def test_failed_fetch_preserves_calendar_and_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config, readme, output = tmp/'positions.json', tmp/'README.md', tmp/'dividends.ics'
            config.write_text(json.dumps({'positions':POS}))
            readme.write_text('<!-- POSITIONS:START -->old<!-- POSITIONS:END -->')
            output.write_text('original calendar')
            with patch.object(runner,'POSITIONS_FILE',config), patch.object(runner,'README_FILE',readme), \
                 patch.object(c,'OUTPUT_FILE',output), patch.object(c,'generate_calendar',side_effect=s.SourceError('all sources failed')):
                with self.assertRaises(s.SourceError):runner.main()
            self.assertEqual(output.read_text(),'original calendar')
            self.assertEqual(readme.read_text(),'<!-- POSITIONS:START -->old<!-- POSITIONS:END -->')

    def test_invalid_position_is_rejected_before_fetching(self):
        for shares in ('NaN', '-1', 'Infinity', '0'):
            with self.subTest(shares=shares), self.assertRaises(ValueError):
                c.validate_positions({'positions':{'IBHG':{**INFO,'shares':shares}}})


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sources = s.DividendSources(Path(self.tmp.name), NOW)
        self.rows = fixture_rows()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.ishares = self.stack.enter_context(patch.object(s,'ishares_rows',side_effect=s.SourceError('offline')))
        self.stock = self.stack.enter_context(patch.object(s,'stockanalysis_rows',side_effect=s.SourceError('offline')))
        self.nasdaq = self.stack.enter_context(patch.object(s,'nasdaq_rows',side_effect=s.SourceError('offline')))
        self.history = self.stack.enter_context(patch.object(s,'dividendhistory_rows',side_effect=s.SourceError('offline')))

    def seed(self, fetched_at=NOW):
        self.sources.cache['funds']['IBHG'] = {
            'identity':{'source':'ishares','portfolio_id':319422}, 'provider':'iShares',
            'fetched_at':fetched_at.isoformat(), 'rows':s.serialize_rows(self.rows)}

    def test_empty_primary_falls_back_to_blackrock(self):
        self.ishares.side_effect = [[], self.rows]
        result = self.sources.fetch('IBHG',INFO)
        self.assertEqual(result[0]['provider'],'BlackRock')
        self.assertFalse(self.stock.called)

    def test_empty_both_official_hosts_fall_back_to_independent_source(self):
        self.ishares.side_effect = [{}, []]
        self.stock.side_effect = None
        self.stock.return_value = self.rows
        result = self.sources.fetch('IBHG',INFO)
        self.assertEqual(result[0]['provider'],'StockAnalysis')
        self.assertEqual(len(self.sources.status['sources']['IBHG']['failures']),2)

    def test_equity_falls_back_to_nasdaq(self):
        self.nasdaq.side_effect = None
        self.nasdaq.return_value = fixture_rows('QQQ')
        result = self.sources.fetch('QQQ',{'source':'stockanalysis','shares':100})
        self.assertEqual(result[0]['provider'],'Nasdaq')
        self.assertFalse(self.ishares.called)

    def test_invalid_money_date_duplicate_and_stale_data_rejected(self):
        variants = [[], [{**self.rows[0],'per_share':float('nan')}, *self.rows[1:]],
                    [{**self.rows[0],'payable_date':date(2000,1,1)}, *self.rows[1:]],
                    [self.rows[0], *self.rows], self.rows[6:]]
        for rows in variants:
            with self.subTest(rows=len(rows)), self.assertRaises(s.SourceError):
                s.validate_rows(rows,'IBHG',INFO,NOW.date())

    def test_missing_known_payment_causes_fallback(self):
        self.seed()
        self.ishares.side_effect = [self.rows[1:], self.rows]
        result = self.sources.fetch('IBHG',INFO)
        self.assertEqual(result[0]['provider'],'BlackRock')

    def test_short_history_cannot_be_used_as_full_year_cash_estimate(self):
        with self.assertRaises(s.SourceError):
            s.validate_rows(self.rows[:6],'IBHG',INFO,NOW.date())

    def test_cache_used_and_age_never_renewed_during_outage(self):
        fetched = NOW-timedelta(days=2)
        self.seed(fetched)
        result = self.sources.fetch('IBHG',{**INFO,'shares':200})
        self.assertTrue(all(row['cached'] for row in result))
        self.assertNotIn('shares',result[0])
        self.assertEqual(self.sources.cache['funds']['IBHG']['fetched_at'],fetched.isoformat())
        self.sources.save()
        restored = s.DividendSources(Path(self.tmp.name),NOW+timedelta(days=6))
        with self.assertRaises(s.SourceError):restored.fetch('IBHG',INFO)

    def test_expired_cache_fails_closed(self):
        self.seed(NOW-timedelta(days=8))
        with self.assertRaises(s.SourceError):self.sources.fetch('IBHG',INFO)

    def test_wrong_identity_cannot_use_cache(self):
        self.seed()
        with self.assertRaises(s.SourceError):self.sources.fetch('IBHG',{**INFO,'portfolio_id':123})

    def test_no_cache_and_all_sources_fail(self):
        with self.assertRaises(s.SourceError):self.sources.fetch('IBHG',INFO)
        self.assertTrue(self.sources.status['sources']['IBHG']['failed'])

    def test_primary_recovery_preferred_over_cache(self):
        self.seed(NOW-timedelta(days=2))
        self.ishares.side_effect = None
        self.ishares.return_value = self.rows
        result = self.sources.fetch('IBHG',INFO)
        self.assertEqual(result[0]['provider'],'iShares')
        self.assertFalse(result[0]['cached'])
        self.assertFalse(self.stock.called)

    def test_malformed_cache_does_not_prevent_online_recovery(self):
        (Path(self.tmp.name)/'last-good.json').write_text('{broken')
        resolver = s.DividendSources(Path(self.tmp.name),NOW)
        self.ishares.side_effect = None
        self.ishares.return_value = self.rows
        self.assertTrue(resolver.fetch('IBHG',INFO))


class ParserTests(unittest.TestCase):
    def test_dividendhistory_excludes_forecasts_and_keeps_actual_payment(self):
        with patch.object(s,'request_bytes',return_value=(FIX/'dividendhistory.html').read_bytes()):
            rows = s.dividendhistory_rows('VOO')
        self.assertEqual(rows[0]['payable_date'],date(2026,6,30))
        self.assertTrue(all(row['ex_date'] <= NOW.date() for row in rows))
        s.validate_rows(rows,'VOO',{'source':'stockanalysis'},NOW.date())

    def test_nasdaq_empty_history_is_a_source_failure(self):
        with patch.object(s,'request_json',return_value={'data':{'dividends':{'rows':None}}}):
            with self.assertRaises(s.SourceError):s.nasdaq_rows('VOO')

    def test_stockanalysis_responsive_headers(self):
        with patch.object(s,'request_bytes',return_value=(FIX/'stockanalysis.html').read_bytes()):
            rows = s.stockanalysis_rows('QQQ')
        self.assertEqual(rows[0]['payable_date'], date(2026,7,10))
        self.assertEqual(rows[0]['per_share'],0.81349)

    def test_wrong_ticker_html_rejected(self):
        with patch.object(s,'request_bytes',return_value=(FIX/'stockanalysis.html').read_bytes()):
            with self.assertRaises(s.SourceError):s.stockanalysis_rows('VOO')

    def test_ishares_rejects_empty_payload_and_misaligned_columns(self):
        with patch.object(s,'request_json',return_value={}):
            with self.assertRaises(s.SourceError):s.ishares_rows('IBHG',319422,s.HOSTS[0])
        data = json.loads((FIX/'ishares.json').read_text())
        data['componentsByNameMap']['fundDownload']['containersByNameMap']['distributions']['dataPointsByNameMap']['exDate']['value'].pop()
        with patch.object(s,'request_json',return_value=data):
            with self.assertRaises(s.SourceError):s.ishares_rows('IBHG',319422,s.HOSTS[0])

    def parsed_schedule(self):
        page = Mock()
        page.extract_text.return_value = (FIX/'monthly-layout.txt').read_text()
        with patch.object(s,'PdfReader',return_value=Mock(pages=[page])):
            return s.parse_schedule(b'test PDF')

    def test_conditional_and_other_table_dates_excluded(self):
        dates = self.parsed_schedule()
        self.assertIn(DEC1,dates)
        self.assertIn(DEC2,dates)
        self.assertNotIn(date(2027,1,5),dates)
        self.assertNotIn(date(2028,1,4),dates)
        self.assertEqual(len(dates),30)

    def test_changed_pdf_layout_fails_closed(self):
        page = Mock()
        page.extract_text.return_value = 'IBHF IBHG IBHK PAY DATE: 5-Jan-27'
        with patch.object(s,'PdfReader',return_value=Mock(pages=[page])):
            with self.assertRaises(s.SourceError):s.parse_schedule(b'invalid')

    def test_schedule_failure_uses_cache_then_history_projection(self):
        dates = self.parsed_schedule()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            resolver = s.DividendSources(Path(tmp),NOW)
            resolver.cache['schedule'] = {'dates':[d.isoformat() for d in dates], 'fetched_at':NOW.isoformat()}
            with patch.object(s,'request_bytes',side_effect=s.SourceError('offline')):
                cached,label = resolver.schedule()
                self.assertEqual(cached,dates)
                self.assertIn('缓存',label)
                resolver.now += timedelta(days=8)
                dates,label = resolver.schedule()
                self.assertEqual(dates,[])
                self.assertEqual(label,'推算支付日')

    def test_screener_failure_does_not_block_history_metrics(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            resolver = s.DividendSources(Path(tmp),NOW)
            with patch.object(s,'request_json',return_value={}):
                self.assertEqual(resolver.screener(['IBHG']),{})


class IntegrationTests(unittest.TestCase):
    def test_two_runs_with_identical_data_do_not_rewrite_calendar(self):
        rows = fixture_rows()
        page = Mock()
        page.extract_text.return_value = (FIX/'monthly-layout.txt').read_text()
        with patch.object(s,'PdfReader',return_value=Mock(pages=[page])):
            dates = s.parse_schedule(b'PDF')
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            with patch.object(s,'ishares_rows',return_value=rows), \
                 patch.object(s.DividendSources,'schedule',return_value=(dates,'官方支付日')), \
                 patch.object(s.DividendSources,'screener',return_value={}), \
                 patch.object(c,'OUTPUT_FILE',root/'dividends.ics'):
                first = c.generate_calendar({'positions':POS},s.DividendSources(root,NOW))
                self.assertTrue(c.write_calendar(first))
                before = c.OUTPUT_FILE.read_bytes()
                second = c.generate_calendar({'positions':POS},s.DividendSources(root,NOW+timedelta(hours=6)))
                self.assertFalse(c.write_calendar(second))
                self.assertEqual(before,c.OUTPUT_FILE.read_bytes())

    def test_fallback_to_dividendhistory_when_nasdaq_has_no_data(self):
        with patch.object(s,'request_bytes',return_value=(FIX/'dividendhistory.html').read_bytes()):
            rows = s.dividendhistory_rows('VOO')
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            resolver = s.DividendSources(Path(tmp),NOW)
            with patch.object(s,'stockanalysis_rows',side_effect=s.SourceError('offline')), \
                 patch.object(s,'nasdaq_rows',return_value=[]), patch.object(s,'dividendhistory_rows',return_value=rows):
                selected = resolver.fetch('VOO',{'shares':100,'source':'stockanalysis'})
                self.assertEqual(selected[0]['provider'],'DividendHistory')

    def test_total_outage_uses_cache_and_expiry_fails_without_overwrite(self):
        rows = fixture_rows()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            resolver = s.DividendSources(root,NOW)
            with patch.object(s,'ishares_rows',return_value=rows):
                resolver.fetch('IBHG',INFO)
            resolver.save()
            cached_bytes = (root/'last-good.json').read_bytes()
            with patch.object(s,'request_bytes',side_effect=s.SourceError('offline')):
                text = c.generate_calendar({'positions':POS},s.DividendSources(root,NOW+timedelta(days=1)))
                logical = '\n'.join(c.unfold(text))
                self.assertIn('DESCRIPTION:IBHG｜100.0股｜$',logical)
                status = json.loads((root/'source-status.json').read_text())
                self.assertTrue(status['sources']['IBHG']['cached'])
                self.assertTrue(status['sources']['schedule']['projected'])
                self.assertEqual(cached_bytes,(root/'last-good.json').read_bytes())
                with self.assertRaises(s.SourceError):
                    c.generate_calendar({'positions':POS},s.DividendSources(root,NOW+timedelta(days=8)))
                self.assertFalse(json.loads((root/'source-status.json').read_text())['success'])
                self.assertEqual(cached_bytes,(root/'last-good.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
