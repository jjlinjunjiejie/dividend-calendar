# Dividend Calendar

Apple Calendar subscription feed for the following iShares iBonds positions:

| Ticker | Shares |
|---|---:|
| IBHF | 127.3954 |
| IBHG | 21,809.3968 |
| IBHH | 2,559.3648 |
| IBHI | 846.3668 |
| IBHJ | 760.3254 |
| IBHK | 1,232.3071 |

The calendar uses each fund's official iShares / BlackRock `fundDownload` distribution data. Events are grouped by payable date and the event title is the combined estimated pre-tax cash amount, for example `$2,975.51`.

## Apple Calendar subscription

Use this subscription URL:

`webcal://raw.githubusercontent.com/jjlinjunjiejie/dividend-calendar/main/dividends.ics`

HTTPS feed URL:

`https://raw.githubusercontent.com/jjlinjunjiejie/dividend-calendar/main/dividends.ics`

GitHub Actions checks for updated distribution data every 6 hours. When iShares publishes a new distribution, `dividends.ics` is rebuilt and committed automatically. Apple Calendar controls its own refresh timing, so changes may not appear instantly.

Amounts are estimates before tax and may differ from actual brokerage cash received because of taxes, broker processing, or position changes.
