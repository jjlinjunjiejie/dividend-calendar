"""Issuer payment dates, independent of whether a cash amount is announced."""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader

from data_sources import SourceError, request_bytes

QQQ_INDEX = ('https://www1.hkexnews.hk/search/titlesearch.xhtml'
             '?category=0&lang=EN&market=SEHK&stockId=1000249051')
QUARTERS = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4}


def pdf_text(payload: bytes) -> str:
    return '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(payload)).pages)


def validate_dates(rows: list[dict], year: int) -> list[dict]:
    if not rows:
        raise SourceError('No official quarterly payment dates')
    seen = set()
    for row in rows:
        cycle = tuple(row['cycle'])
        pay = date.fromisoformat(row['payable_date'])
        if cycle[0] != year or cycle[1] not in range(1, 5) or cycle in seen:
            raise SourceError('Invalid or duplicate official quarter')
        start = date(year, cycle[1] * 3 - 2, 1)
        if not 0 <= (pay - start).days <= 150:
            raise SourceError('Official payment outside distribution quarter window')
        seen.add(cycle)
    return rows


def parse_vanguard(text: str, year: int) -> list[dict]:
    # Match both CUSIP and exact ticker; VOOG/VOOV must never be selected.
    match = re.search(r'S&P\s+500\s+ETF\s+922908363\s+VOO\b', text)
    if not match:
        raise SourceError('Vanguard VOO identity missing')
    block = re.match(r'(?:\s*\d{2}/\d{2}/\d{2})+', text[match.end():])
    tokens = re.findall(r'\d{2}/\d{2}/\d{2}', block[0] if block else '')
    if len(tokens) not in (12, 15):
        raise SourceError('Incomplete Vanguard quarterly schedule')
    rows = []
    for i in range(0, len(tokens), 3):
        record, ex, pay = [datetime.strptime(t, '%m/%d/%y').date() for t in tokens[i:i+3]]
        if ex.year != year or record != ex or not 0 <= (pay-ex).days <= 14:
            raise SourceError('Invalid Vanguard date columns')
        if ex.month == 12 and ex.day >= 25:
            continue  # year-end supplemental slot, not a fifth regular dividend
        if ex.month not in (3, 6, 9, 12):
            raise SourceError('Unexpected Vanguard regular quarter')
        rows.append({'cycle': [year, ex.month // 3], 'payable_date': pay.isoformat()})
    if len(rows) != 4:
        raise SourceError('Vanguard requires four regular quarters')
    return validate_dates(rows, year)


def parse_qqq(text: str, year: int) -> list[dict]:
    text = re.sub(r'\s+', ' ', text)
    if not re.search(r'Invesco QQQ (?:Trust(?:SM)?|ETF)\b', text):
        raise SourceError('Invesco QQQ identity missing')
    quarter = re.search(r'(first|second|third|fourth) quarter distribution.*?calendar year (\d{4})',
                        text, re.I)
    if not quarter or int(quarter[2]) != year:
        raise SourceError('QQQ quarter/year missing')
    token = r'\d{1,2} [A-Za-z]+ \d{4}'
    table = re.search(r'Ex-dividend date Declaration date Record date Payment date\s+'
                      r'(' + token + r')\s+(' + token + r')\s+(' + token + r')\s+(' + token + r')', text)
    if table:
        pay_text = table[4]
    else:
        # Follow-up amount announcements explicitly identify the US DTC date.
        match = re.search(r'payment date is (' + token + r').{0,160}Depository Trust Company', text)
        if not match:
            raise SourceError('QQQ payment date missing')
        pay_text = match[1]
    pay = datetime.strptime(pay_text, '%d %B %Y').date()
    return validate_dates([{'cycle': [year, QUARTERS[quarter[1].lower()]],
                            'payable_date': pay.isoformat()}], year)


def qqq_links(html: bytes, year: int, quarter: int) -> list[str]:
    result, seen = [], set()
    for link in BeautifulSoup(html, 'html.parser').find_all('a', href=True):
        title = re.sub(r'\s+', ' ', link.get_text(' ', strip=True)).lower()
        match = re.fullmatch(r'(first|second|third|fourth) quarter distribution announcement', title)
        url = urljoin(QQQ_INDEX, link['href'])
        parts = urlparse(url)
        if (not match or QUARTERS[match[1]] < quarter or parts.scheme != 'https'
                or parts.netloc != 'www1.hkexnews.hk'
                or not re.fullmatch(rf'/listedco/listconews/sehk/{year}/\d{{4}}/\d+\.pdf', parts.path)):
            continue
        if match[1] not in seen:  # directory is newest first: latest revision wins
            result.append(url)
            seen.add(match[1])
    if not result:
        raise SourceError('No current QQQ official announcements in directory')
    return result


def fetch_dates(ticker: str, year: int, quarter: int) -> list[dict]:
    if ticker == 'VOO':
        url = f'https://advisors.vanguard.com/content/dam/fas/pdfs/DIVDAT_{year}.pdf'
        return [{**r, 'url': url} for r in parse_vanguard(pdf_text(request_bytes(url, 'application/pdf')), year)]
    if ticker == 'QQQ':
        rows = []
        for url in qqq_links(request_bytes(QQQ_INDEX, 'text/html'), year, quarter):
            rows.extend({**r, 'url': url} for r in parse_qqq(pdf_text(request_bytes(url, 'application/pdf')), year))
        return validate_dates(rows, year)
    raise SourceError(f'Unsupported official equity schedule: {ticker}')
