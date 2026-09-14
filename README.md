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

The calendar uses official iShares / BlackRock distribution data for the iShares positions and StockAnalysis dividend data for QQQ/VOO. Events are grouped by payable date and the event title is the combined estimated pre-tax cash amount, for example `$2,975`.

Displayed dividend cash amounts are truncated to whole USD amounts and are not rounded. Per-share dividend values, prices, yields, and market values keep their existing precision.

The positions table above is generated from `positions.json` whenever the calendar update runs, so position changes are reflected in this README automatically.

## Apple Calendar subscription

Use this subscription URL:

`webcal://raw.githubusercontent.com/jjlinjunjiejie/dividend-calendar/main/dividends.ics`

HTTPS feed URL:

`https://raw.githubusercontent.com/jjlinjunjiejie/dividend-calendar/main/dividends.ics`

GitHub Actions checks for updated distribution data every 6 hours. When dividend data or positions change, `dividends.ics` is rebuilt and generated README position data is committed automatically. Apple Calendar controls its own refresh timing, so changes may not appear instantly.

Amounts are estimates before tax and may differ from actual brokerage cash received because of taxes, broker processing, or position changes.
