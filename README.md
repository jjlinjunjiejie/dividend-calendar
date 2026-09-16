# Dividend Calendar

Apple Calendar subscription feed for the following portfolio positions:

<!-- POSITIONS:START -->
| Ticker | Shares |
|---|---:|
| IBHG | 21,809.3968 |
| IBHH | 2,559.3648 |
| IBHI | 846.3668 |
| IBHJ | 760.3254 |
| IBHK | 1,232.3071 |
| QQQ | 600.6061 |
| VOO | 700.4031 |
<!-- POSITIONS:END -->

The calendar prefers official iShares / BlackRock distribution data for the iShares positions and StockAnalysis dividend data for QQQ/VOO, with validated fallback providers described below. Events are grouped by payable date. If multiple holdings pay on the same date, their dividends are combined into one calendar event.

The calendar shows the next 24 months of dividend events. Official iShares payment dates are used when available; months beyond the currently published official schedule are projected using the monthly cadence, including two December payments and no regular January payment. VOO payment dates prefer Vanguard's official annual schedule, while QQQ payment dates prefer official Invesco QQQ distribution announcements; unpublished quarters fall back to the recent validated quarterly payment pattern.

Displayed dividend amounts apply a 10% withholding tax, so the calendar shows 90% of the estimated or announced gross dividend. Event titles show the combined after-tax amount as whole digits with no dollar sign or thousands separator. If any included payment date is still projected, the title is prefixed with the compact `⋄` marker, for example `⋄1157`; official or actual payment dates keep the plain numeric title. The memo lists one line per ticker in the format `IBHG｜21,809.3股｜$2,014.3`, followed by `预扣税10%`. When any included payment date is projected, the final line is `预扣税10%｜预计本日发放`. An official date needs no certainty label, even if the amount is estimated. Monthly estimates prefer NAV and SEC yield; quarterly estimates use validated trailing cash distributions directly.

Calendar titles are truncated to whole USD amounts without rounding. Memo share counts and dividend amounts are truncated to one decimal place without rounding. Memo dividend amounts keep the `$` symbol and thousands separators.

The positions table above is generated from `positions.json` whenever the calendar update runs, so position changes are reflected in this README automatically.

## Apple Calendar subscription

Use this subscription URL:

`webcal://raw.githubusercontent.com/jjlinjunjiejie/dividend-calendar/main/dividends.ics`

HTTPS feed URL:

`https://raw.githubusercontent.com/jjlinjunjiejie/dividend-calendar/main/dividends.ics`

GitHub Actions checks for updated distribution data every 6 hours. When dividend data or positions change, `dividends.ics` is rebuilt and generated README position data is committed automatically. Apple Calendar controls its own refresh timing, so changes may not appear instantly.

Displayed amounts assume a fixed 10% withholding rate and may differ from actual brokerage cash received because of broker processing, tax treatment, or position changes.

## Run locally

Requires Python 3.12 or newer:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python run_update.py
```

Both entry points now generate the same 24-month, after-tax calendar. Amounts are
calculated with `Decimal`; rounding is deferred until the final display.

## Source validation and fallback

| Data | Primary | Online fallback | Last resort |
|---|---|---|---|
| iShares distributions | iShares product API | BlackRock API → StockAnalysis → Nasdaq → DividendHistory | Verified snapshot, at most 7 days old |
| QQQ / VOO distributions | StockAnalysis | Nasdaq → DividendHistory | Verified snapshot, at most 7 days old |
| Regular iShares payment dates | iShares PDF | BlackRock PDF | Verified PDF dates cached for at most 7 days; otherwise explicitly projected dates |
| VOO payment dates | Vanguard annual distribution schedule | Verified issuer schedule cache | Recent validated quarterly pattern, explicitly projected |
| QQQ payment dates | Invesco QQQ official distribution announcements | Verified issuer announcement cache | Recent validated quarterly pattern, explicitly projected |
| iShares NAV and yield | iShares screener | BlackRock screener | Estimate directly from validated trailing cash distributions |

DividendHistory forecasts marked unconfirmed/estimated are excluded. Nasdaq may return no history for some ETFs, including VOO during live verification; DividendHistory supplies a further independent fallback.

Each holding switches independently. A successful HTTP response is insufficient:
responses must pass ticker/portfolio, amount, date order, duplicate, minimum
history, trailing-year coverage, freshness, and previously known record checks. Dividend-history age limits
are 75 days for monthly funds (allowing the December-to-February gap) and 150 days
for quarterly funds. These detect stale responses; they cannot prove that a source
has published every new announcement. Primary providers are retried first on every
run, so recovery automatically restores the preferred source.

Source snapshots live in `.cache/dividend-calendar/last-good.json`, outside Git.
GitHub Actions restores/saves that file using Actions Cache. Its original successful
fetch time is retained during outages; restoring or reusing a snapshot does not
extend its seven-day lifetime. Cache eviction can remove this last resort. If all
online dividend providers and the valid cache fail, the update fails and leaves the
existing calendar and README intact.

The memo keeps the compact ticker/share/after-tax-amount format plus a final tax/date-certainty note.
Source failures, provider selections, cache usage, and schedule fallback status
are recorded in `.cache/dividend-calendar/source-status.json`, also uploaded as the
`dividend-source-status` workflow artifact (retained for seven days).

The monthly schedule follows the [official iShares distribution schedule](https://www.ishares.com/us/literature/shareholder-letters/isharesandblackrocketfsdistributionschedule.pdf):
January's regular distribution is brought forward into December. Both December
payments receive independent monthly estimates. The conditional excise-distribution
column is excluded from forecasts; a subsequently announced payment is still included
from the distribution history. Official announcements replace estimates by distribution
cycle, even when their payment date moves.

Unchanged events retain `DTSTAMP`, `LAST-MODIFIED`, and `SEQUENCE`. Changed events
increment `SEQUENCE`; unchanged calendar bytes are not rewritten. Snapshot refreshes
therefore do not create generated-file commits.

Implementation details and operational limits: [备用源系统设计](SOURCE_DESIGN.md).
