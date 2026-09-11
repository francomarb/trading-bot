# SMA Crossover Entry-Quality Audit (`11.70`)

**Status:** Closed 2026-09-11 — retain current behavior. No paper behavior
change is authorized by this workstream.

## Question

Four recent completed SMA paper lifecycles stopped near −1R, and several entry
fills left the normal 2×ATR stop close to SMA50. The audit asks whether that is
evidence for a better entry/stop rule or ordinary trend-following loss
clustering.

## Fixed policies

The control, structure-stop, and pullback definitions were fixed before the
first full result run. Review then restored the extension-cap alternative named
in the original investigation; its geometry-defined threshold was not selected
from a sweep. The review also added a stricter paired-CI requirement to the
decision rule. All definitions and gates below are frozen for future reuse.

| Policy | Entry | Initial stop | Sizing |
|---|---|---|---|
| `control` | Production-valid 20/50 cross, next-session open | Fill − 2× signal-day ATR14 | 1R fixed risk |
| `structure_stop` | Same as control | Lower of control stop and signal-day SMA50 − 0.5×ATR14 | 1R fixed risk; a wider stop receives fewer shares |
| `pullback` | Five-session buy limit at signal close − 0.5×ATR14; cancel unfilled or after the bullish 20/50 state fails | Fill − 2× ATR14 available before the fill session | 1R fixed risk |
| `extension_cap` | Same as control, but skip a next-open fill more than 2×ATR14 above signal-day SMA50 | Same as control | 1R fixed risk |

The extension-cap arm restores the fixed-extension alternative named in the
original investigation. Its 2×ATR threshold comes directly from the geometry
under review—the point where the normal stop reaches SMA50—and was not selected
from a parameter sweep.

Every arm uses the production death-cross exit, realistic gap-through-stop
pricing, and same-symbol non-overlap. An open position at a period boundary is
marked at that period's last close.

## Production fidelity

Mirrored exactly from current code:

- 20/50 crossover and death-cross signal logic;
- stock close above SMA200 (including the runtime fail-open warmup behavior);
- 10-day median volume above 30-day median volume;
- the shared, production-parity SPY regime classifier and SMA's
  `TRENDING`/`RANGING` allow-list;
- next-open market fill, fill-anchored static stop, and same-session stop risk;
- sector COLD is warning-only in the active slot and therefore does not block.

Known limitations:

- The runtime yfinance earnings source is not a point-in-time-complete historical
  calendar. The script accepts a `symbol,date` CSV; without one it labels the
  blackout unavailable and follows production's fail-open behavior.
- The frozen 56-name universe is the 2026-09-10 production list. Replaying it
  into earlier years has survivor/selection bias. Comparisons between policies
  are still paired on the same universe, but the absolute strategy expectancy
  is not a clean historical portfolio estimate.
- Reported drawdown is the closed-trade/period-mark fixed-R sequence, not a
  daily marked-to-market portfolio curve. Sleeve, gross, cash, and notional caps
  are deliberately excluded so every admitted trade receives the same 1R.

## Sample split and decision rule

- Development: 2017-01-01 through 2022-12-31.
- Held out: 2023-01-01 through the pinned audit end (initially 2026-09-04).
- The periods run independently; a development position is marked at the
  development boundary and cannot leak a held-out exit into selection.

A variant can justify a separate paper-change proposal only if all are true:

1. expectancy improves versus control in both development and held-out data;
2. held-out realized-sequence maximum drawdown is no worse;
3. it retains at least 60% of control's held-out trade count;
4. it has at least 30 held-out trades; and
5. the held-out 95% bootstrap confidence interval for paired,
   opportunity-level variant-minus-control R is entirely above zero.

Pairing assigns 0R when a policy declines a valid signal, so a selective policy
cannot improve its statistical comparison by averaging only its fills. The
bootstrap uses the existing project helper with a fixed seed. It is an iid
bootstrap and therefore retains the documented autocorrelation limitation.

Passing this rule does not authorize a strategy change. It only earns a
reviewed paper experiment. Failing it leaves the current bot unchanged.

## Reproduce

```bash
./venv/bin/python -m scripts.sma_entry_quality_audit
```

The command reads local `data/historical/<feed>/*_1Day_all.parquet` caches and
does not refresh them. Missing symbols and period coverage are printed.

## Initial fixed-policy run — 2026-09-10

Feed: SIP. Frozen universe: 56 names. End: 2026-09-04. Local cache is now
complete for all 56 names; SNDK, ECG, and DASH have no development-period bars
because their available history starts later. Earnings coverage was unavailable
and therefore failed open, as disclosed above.

| Period / policy | N | Mean R | Median R | Total R | Max DD | Stop rate | Control losses not entered | Control winners not entered |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Development control | 172 | +0.18 | −1.00 | +30.66 | 20.52 | 62.8% | — | — |
| Development structure stop | 172 | +0.08 | −0.62 | +13.89 | 15.46 | 46.5% | 0 | 0 |
| Development pullback | 132 | +0.29 | −1.00 | +38.52 | 13.86 | 58.3% | 24 | 16 |
| Development extension cap | 75 | +0.18 | −1.00 | +13.13 | 8.79 | 53.3% | 70 | 27 |
| Held-out control | 149 | +3.56 | −1.00 | +531.02 | 20.44 | 56.4% | — | — |
| Held-out structure stop | 149 | +2.69 | −0.28 | +400.78 | 11.57 | 32.9% | 0 | 0 |
| Held-out pullback | 98 | +1.35 | −1.00 | +132.71 | 14.29 | 49.0% | 21 | 30 |
| Held-out extension cap | 57 | +7.08 | −1.00 | +403.80 | 6.79 | 50.9% | 60 | 32 |

No alternative passes the review-hardened rule:

- The structure-aware stop reduces stop frequency and realized-sequence
  drawdown, but lowers expectancy in both periods. Fixed-risk sizing correctly
  gives its wider stops fewer shares; the apparent comfort is purchased by
  shrinking exposure to the winners.
- The pullback improves development expectancy, but reverses sharply in held-out
  data. It retains 65.8% of held-out control trades. Among control trades it
  declines, 21 were losses and 30 were winners. Randomly declining the same 51
  control trades would be expected to omit about 32.5 losses and 18.5 winners;
  the probability of randomly omitting at least 30 winners is below 0.001. The
  pullback selection was materially worse than chance in held-out data, not
  evidence of useful loser avoidance.
- The fixed extension cap retains only 38.3% of held-out trades, below the 60%
  gate. Its mean R is flattered by retaining the single dominant runner, while
  its paired held-out delta CI is negative. Its omitted winner/loss mix is
  statistically ordinary at the same skip rate (p=0.741).

Paired held-out variant-minus-control 95% CIs are:

- structure stop: [−2.07R, −0.19R];
- pullback: [−7.91R, +0.28R];
- extension cap: [−1.57R, −0.25R].

No arm establishes a positive held-out improvement. In particular, the
pullback conclusion no longer rests only on the direction of a point estimate
dominated by one outlier.

The absolute held-out expectancy must not be read as a portfolio forecast. Its
median trade is still −1R, and one 354.69R winner contributes 66.8% of held-out
control net R. Development is also concentrated: its largest winner contributes
70.3% of net R. This is consistent with the strategy's documented convex payoff,
but the 2026-selected universe makes the magnitude selection-biased.

The original four-stop incident is not rare under the historical control base
rate. Development contains 28 rolling four-stop windows (16.6% of eligible
four-trade windows; maximum streak 8), and held-out data contains 33 (22.6%;
maximum streak 9). Four recent near-−1R stops therefore do not independently
show that entry logic has degraded.

The related optimization note still ranks watchlist composition as the highest
leverage SMA research area. That is separate from `11.70`: this audit tests
entry/initial-stop geometry on a frozen list and does not validate the list's
historical membership or scanner ranking.

**Final verdict:** retain the current entry and stop behavior. The fixed
variants fail on the evidence already available. Historical earnings coverage
and a defensible historical-universe reconstruction would be required before
approving a future positive change, but they are not reasons to continue tuning
variants that already failed. Reopen only if new paper evidence contradicts the
accepted clustered-loss / rare-runner payoff shape, or a materially different
entry proposal is pre-registered.
