# RSI Reversion Starvation Audit - 2026-08-17

> Historical diagnosis of the prior RSI14 + stacked-filter production design.
> As of 2026-08-21, active RSI production has been reset to RSI3 `<15`,
> quick exits, all regimes, and only stock SMA200/liquidity edge gates. See
> [`rsi_reversion_strategy.md`](rsi_reversion_strategy.md) and
> [`RSI-edge-filter.md`](RSI-edge-filter.md) for the current spec.

## Scope

This audit explains why `rsi_reversion` has produced no filled trades during
the current paper run. It uses three sources:

- `data/trades.db` trade, lifecycle, and `strategy_lifecycle_counters` tables
- retained plain-text bot logs: `logs/forward_test*.log`
- current production wiring in `forward_test.py`, `config/settings.py`, and the
  RSI filter/strategy modules

Log counts below are deduplicated by timestamp, symbol, and message. They are
per-cycle filter evaluations, not raw-entry candidates. The SQLite lifecycle
counters are the source of truth for raw-candidate gate attribution.

## Executive Summary

RSI starvation is real, but it is not one hidden allocator/risk bug.

1. Raw signal frequency is very low. Since lifecycle counters began capturing
   RSI candidates, the bot recorded only 7 raw RSI entries.
2. The edge stack blocked 6 of those 7 raw entries. Regime, sleeve, and risk
   did not block any RSI raw entries.
3. The only raw entry that passed filters and risk was CAT on 2026-07-30. It
   failed on the broker submission path before Alpaca accepted the order, and
   startup reconciliation later confirmed the client order ID was unknown to
   Alpaca.

The practical diagnosis is: scarce raw RSI crosses + protective edge filters +
one broker-connectivity miss.

## Production Gate Attribution

`strategy_lifecycle_counters` currently shows the following for
`rsi_reversion`:

| Period | Raw | Regime Blocked | Edge Blocked | Sleeve Blocked | Risk Blocked | Submitted | Filled |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-06-08 to 2026-06-15 | 2 | 0 | 2 | 0 | 0 | 0 | 0 |
| 2026-06-22 to 2026-06-29 | 4 | 0 | 4 | 0 | 0 | 0 | 0 |
| 2026-07-27 to 2026-08-03 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| **Total** | **7** | **0** | **6** | **0** | **0** | **0** | **0** |

Interpretation:

- `raw=7`: the raw RSI cross-below-30 setup barely appeared.
- `edge=6`: when it did appear, filters were the dominant blocker.
- `regime=0`: the engine-level `TRENDING` / `RANGING` gate did not explain the
  dry spell.
- `sleeve=0`, `risk=0`: capital allocation and risk sizing did not explain the
  dry spell.
- `submitted=0`: no RSI order was accepted by the broker. The one attempted
  CAT order raised before Alpaca returned an order ID, so it was not counted as
  submitted.

## The One Passed Candidate

On 2026-07-30, CAT passed the full entry stack:

- `RSI_FILTER_ALLOWED CAT -- SPY=True earnings=True liquid=True no_active_breakdown=True`
- `[rsi_reversion] CAT ... entry=True`
- `risk approved CAT: 3 shares @ $782.98, stop $701.54`
- `placing limit buy 3 CAT (stop $701.54, client_id=rsi_reversion-782dafe0b9)`

Immediately after submission, the broker call raised:

- `RemoteDisconnected('Remote end closed connection without response')`

The lifecycle substrate later recorded:

- `entry_order_id` remained `NULL`
- lifecycle order `status='rejected'`
- `filled_qty=0`
- startup sweep: broker did not know `rsi_reversion-782dafe0b9`

Conclusion: CAT was a real missed RSI opportunity, but it was an order
submission reliability miss, not a strategy/risk/filter rejection.

## Per-Cycle Filter Hit Counts

Retained `forward_test*.log` files contain:

| Metric | Count |
|---|---:|
| `RSI_FILTER_ALLOWED` evaluations | 2,885 |
| `RSI_FILTER_BLOCKED` evaluations | 319 |
| Filter-evaluation block rate | 10.0% |
| `entry=True` lines after filtering | 1 |

Reason hits can overlap when multiple filters fail on the same evaluation:

| Gate | Retained Log Hits | Current Setting | Rationale | Interpretation |
|---|---:|---|---|---|
| Active breakdown | 182 | `new_low_window=20`; current rule blocks only a new 20-day low below the stock's 200 SMA | Avoid buying a company that is actively breaking down rather than merely oversold | Largest retained blocker. May/June counts include older log wording from before the 2026-06 active-breakdown refinement, so treat this as "breakdown-family" evidence rather than exact post-fix performance. |
| Earnings blackout | 130 | `days_before=3`, `days_after=2` | Avoid binary event risk where a dip can become a gap through the stop | Dominant in July/August retained logs. This is protective and plausible, but it can block the few raw RSI crosses because oversold often appears around earnings shocks. |
| SPY 50 SMA band | 29 | `RSI_SPY50_TOLERANCE_PCT=0.01`; SPY may be up to 1% below its 50 SMA | Do not fade individual selloffs when the broad market is materially below intermediate trend | Much less frequent after the 1% band. July 29 blocked CAT one day before the passed July 30 CAT attempt; the band is still relevant, but not the main retained blocker. |
| Liquidity floor | 0 | 20-day average dollar volume >= $10M | Avoid partial or poor-quality fills on a passive limit-entry mean-reversion strategy | Not currently causing starvation on this watchlist. |
| Sector momentum | 56 | RSI uses `SectorMomentumFilter(policy='block', score_threshold=-3)` | Avoid clustered dip-buying in a genuinely falling sector | Separate from `RSI_FILTER_BLOCKED` lines and logged as `SECTOR GATE [block]`. Retained hits were concentrated in communications and discretionary names. |

Retained block-hit timing by month:

| Month | Active Breakdown | Earnings | SPY50 Band |
|---|---:|---:|---:|
| 2026-05 | 40 | 15 | 0 |
| 2026-06 | 126 | 12 | 0 |
| 2026-07 | 15 | 69 | 29 |
| 2026-08 | 1 | 34 | 0 |

Top symbols by `RSI_FILTER_BLOCKED` evaluation count:

| Symbol | Count |
|---|---:|
| META | 26 |
| AMZN | 22 |
| MSTR | 21 |
| CIEN | 20 |
| MSFT | 19 |
| MA | 18 |
| HD | 17 |
| MCD | 15 |
| SPG | 14 |
| CCK | 14 |

## Current Filter Settings And Rationale

| Layer | Production Setting | Why It Exists | Starvation Read |
|---|---|---|---|
| Raw RSI entry | RSI(14) crosses from `>=30` to `<30` | Fire once when the stock first becomes oversold; do not re-fire every day it remains oversold | This is the first scarcity source. The crossing rule is deliberate but naturally sparse. |
| Regime | `TRENDING`, `RANGING` only | Structural BEAR/VOLATILE markets make long-only dip buying dangerous | Counters show this did not block RSI raw entries in the captured window. |
| SPY50 band | SPY close must be above `0.99 * SMA50` | Keep the RSI-specific intermediate market trend check without duplicating the regime detector's structural BEAR veto | Not the main retained blocker after the 1% band, but still worth paper-watching because it can block isolated high-value windows. |
| Earnings blackout | 3 calendar days before, 2 after | Earnings gaps can invalidate "oversold" mean reversion before the trade can work | Strong blocker in retained logs. It is risk-rational, but it collides with sparse raw setup frequency. |
| Liquidity | 20-day average dollar volume >= $10M | Passive limit orders need fillable names | No retained starvation evidence. |
| Active breakdown | New 20-day low and below 200 SMA | Distinguish a normal pullback from a falling-knife breakdown | Biggest retained blocker family. The current below-200-SMA refinement is more reasonable than the older blanket new-low gate, but the family remains the main protection/cadence trade-off. |
| Sector momentum | Prior RSI14 wiring blocked only at score <= -3 | Avoid correlated dip buys when a sector is in genuine freefall; looser than the global COLD score <= -2 threshold | Retained sector blocks exist but are not the dominant explanation. Active RSI3 no longer uses a sector gate. |

## Documentation And Parity Notes

- `docs/rsi_reversion_strategy.md` and `docs/strategies.md` previously still
  said the RSI watchlist had 29 names. The current `RSI_WATCHLIST` has 30 names
  after NFLX was added.
- Some June research reports reference sector `score <= -2` in backtest
  harnesses. The prior live RSI14 wiring used `score_threshold=-3`, which was
  less restrictive and intentionally mean-reversion-specific. The active RSI3
  production reset no longer uses a sector momentum gate.
- `RSI_FILTER_BLOCKED` logs are per-cycle filter decisions, not raw-entry
  candidate reason rows. The lifecycle counter table gives candidate-level
  attribution, but does not persist structured reasons. If future audits need
  exact "which filter blocked each raw entry" answers, persist candidate-level
  edge reasons alongside `strategy_lifecycle_counters`.

## Operational Follow-Ups

1. Treat CAT 2026-07-30 as an order-submission reliability miss. The current
   substrate correctly reconciled it as never accepted, but the entry
   opportunity was lost.
2. Continue 11.23 paper-watch under the RSI3 reset: the evidence question is now
   whether the simplified signal produces enough clean candidates and fills
   without uncontrolled clustered dip-buying.
3. Consider structured per-candidate reason persistence before further filter
   loosening, so decisions are based on raw-entry blockers rather than
   per-cycle filter noise.
