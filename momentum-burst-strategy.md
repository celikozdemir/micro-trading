Below is a practical, Binance-friendly blueprint that’s designed to catch very short-lived momentum pops (seconds to tens of seconds), while filtering out most noise.

⸻

Data you should use (Binance)

Use WebSockets, not REST, for signals:
	1.	aggTrade stream (per symbol)

	•	Gives you actual prints: price, quantity, maker/taker side proxy.

	2.	depth stream (order book)

	•	Use @depth@100ms if you can handle it; otherwise lower frequency.

	3.	Optional: mark price / funding / index not needed for micro-bursts.

⸻

Micro-burst definition (what you’re trying to detect)

A “micro burst” is typically:
	•	Trade intensity spike (trades/sec jumps)
	•	Aggressive flow imbalance (taker buys ≫ taker sells, or vice versa)
	•	Order book thinning + top-of-book imbalance
	•	Fast price displacement over a very short horizon
	•	Followed by either continuation (your profit) or snapback (your stop)

So we build signals around these 4.

⸻

Core signal set (fast + robust)

1) Trade Intensity Spike

Maintain a rolling window (event-time) for the last N seconds (e.g., 1.0s, 2.5s, 5.0s).

Compute:
	•	trades_per_sec
	•	notional_per_sec (Σ price*qty per sec)

Trigger condition:
	•	notional_per_sec_now > k * median(notional_per_sec_last_60s)
	•	k typical: 3–8 depending on symbol liquidity.

2) Aggressive Flow Imbalance (AFI)

From aggTrade, approximate aggressor:
	•	If buyer is maker → sell aggressor
	•	Else buy aggressor (Binance aggTrade has m flag)

Compute in last 1–3 seconds:
	•	buy_notional vs sell_notional
	•	afi = (buy_notional - sell_notional) / (buy_notional + sell_notional)

Trigger:
	•	Long burst: afi > +0.35 (tune)
	•	Short burst: afi < -0.35

3) Micro Volatility Expansion (Displacement)

Compute a super-short “micro ATR” from mid-price returns:
	•	r_t = log(mid_t / mid_{t-Δ}) for Δ like 100ms–250ms
	•	sigma_fast = EWMA(|r_t|, half-life 1–2s)
	•	sigma_slow = EWMA(|r_t|, half-life 30–60s)

Trigger:
	•	sigma_fast > 2.5 * sigma_slow
	•	AND price moved at least X ticks in last 1–2s.

4) Top-of-book Imbalance + Thinning

From order book, compute:
	•	imb = (bid_qty_topK - ask_qty_topK) / (bid_qty_topK + ask_qty_topK) for K levels (e.g., 5 or 10)
	•	spread = best_ask - best_bid
	•	“thinning” = topK depth drops vs normal

Trigger (for long):
	•	imb > +0.2 AND spread not exploding
	•	thinning on ask side (asks getting pulled) is a strong add-on

⸻

Entry logic (micro-burst “gated” trigger)

You want a gate: require multiple signals at once so you don’t trade random noise.

Long entry gate example

Enter LONG when all true within last 500ms–1500ms:
	1.	intensity_spike == true
	2.	afi > +0.35
	3.	sigma_fast > 2.5 * sigma_slow
	4.	mid breaks above last 1–2s high (tiny breakout confirmation)

Short is symmetric.

Execution style
	•	For micro-bursts you usually use market or aggressive limit (crossing 1–2 ticks).
	•	Keep size small enough that slippage doesn’t destroy the edge.

⸻

Exit logic (this is where micro-burst PnL is made)

Micro bursts die fast. Exits must be mechanical.

Stop (tight)

Use whichever is tighter:
	•	stop = entry - 0.6 * microATR_2s (long)
	•	OR “structure stop”: below the pre-burst mid by a few ticks

Take profit

Use 2-stage:
	•	TP1: +0.8 * microATR_2s (take 50–70%)
	•	Runner: trail with:
	•	mid < EMA(mid, 1–2s) (exit), or
	•	AFI flips sign (flow reversal), or
	•	intensity collapses (notional/sec falls below threshold)

Time stop (mandatory)

If not in profit by 2–8 seconds (depends on symbol), exit.
Micro bursts that don’t go immediately often mean you’re holding the top.

⸻

“Do not trade” filters (prevents death by chop)

Add these filters or you’ll get chewed up:
	1.	Spread filter
If spread widens above a threshold, skip.
	2.	Book instability filter
If best bid/ask flickers too hard (quote stuffing), require stronger confirmation.
	3.	Cool-down
After a trade, enforce cooldown 2–10s to avoid re-entry churn.
	4.	Regime filter
If sigma_slow is extremely low (dead market) OR extremely high (chaotic), adjust thresholds or pause.

⸻

Symbol selection (important)

Micro-burst works best on:
	•	BTCUSDT, ETHUSDT
	•	Very liquid alts (top volume) during active hours

Avoid thin symbols: bursts are mostly manipulation + spread traps.

⸻

Practical latency notes (your Japan server helps)

Even with low latency, your edge is mostly:
	•	faster detection + faster cancel/replace
	•	not “beating HFT” (Binance matching is fast; you’re optimizing reaction)

Make sure you:
	•	Use single persistent WS connections (no reconnect loops)
	•	Put strategy + execution in same process (or shared memory)
	•	Avoid heavy logging on hot path
	•	Use Binance listenKey only for account updates, not signal

⸻

Minimal implementation architecture
	•	MarketData: WS aggTrade + depth → ring buffers (lock-free if possible)
	•	FeatureEngine: computes intensity, AFI, sigma_fast/slow, imbalance
	•	SignalGate: triggers entry/exit state machine
	•	ExecutionEngine:
	•	place order
	•	immediate protection stop (or OCO if you use it)
	•	modify/cancel if needed
	•	RiskManager:
	•	max position
	•	max loss per hour/day
	•	max trades per minute
	•	kill-switch on abnormal slippage

⸻

Recommended first “working” parameter set (starting point)

For BTCUSDT / ETHUSDT:
	•	Windows:
	•	intensity window: 1.0s and 3.0s
	•	AFI window: 1.5s
	•	sigma_fast HL: 1.5s
	•	sigma_slow HL: 45s
	•	order book K: 10 levels
	•	Thresholds:
	•	intensity spike: > 5× median(60s)
	•	AFI: > +0.35 (long), < -0.35 (short)
	•	sigma_fast / sigma_slow: > 2.5
	•	time stop: 5s
	•	cooldown: 4s
