# Dividend Calendar

Apple Calendar subscription feed for the following portfolio positions:

<!-- POSITIONS:START -->
| Ticker | Shares |
|---|---:|
| IBHF | 127.3954 |
| IBHG | 21,809.3968 |
| IBHH | 2,559.3648 |
| IBHI | 846.3668 |
| IBHJ | 760.3254 |
| IBHK | 1,232.3071 |
| QQQ | 600.6061 |
| VOO | 700.4031 |
<!-- POSITIONS:END -->

The calendar uses official iShares / BlackRock distribution data for the iShares positions and StockAnalysis dividend data for QQQ/VOO. Events are grouped by payable date. If multiple holdings pay on the same date, their dividends are combined into one calendar event.

Displayed dividend amounts apply a 10% withholding tax, so the calendar shows 90% of the estimated or announced gross dividend. Event titles show the combined after-tax amount as plain integer digits with no dollar sign or thousands separator. The memo shows each ticker, the current share count, and its after-tax dividend amount.

Calendar titles are truncated to whole USD amounts without rounding. Memo share counts and dividend amounts are truncated to one decimal place without rounding. Memo dividend amounts keep the `$` symbol and thousands separators.

The positions table above is generated from `positions.json` whenever the calendar update runs, so position changes are reflected in this README automatically.

## Apple Calendar subscription

Use this subscription URL:

`webcal://raw.githubusercontent.com/jjlinjunjiejie/dividend-calendar/main/dividends.ics`

HTTPS feed URL:

`https://raw.githubusercontent.com/jjlinjunjiejie/dividend-calendar/main/dividends.ics`

GitHub Actions checks for updated distribution data every 6 hours. When dividend data or positions change, `dividends.ics` is rebuilt and generated README position data is committed automatically. Apple Calendar controls its own refresh timing, so changes may not appear instantly.

Displayed amounts assume a fixed 10% withholding rate and may differ from actual brokerage cash received because of broker processing, tax treatment, or position changes.
