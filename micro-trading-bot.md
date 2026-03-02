# Binance Microstructure Bot — Structured Development Plan (No Polymarket)

> Goal: Build a fully automated, rules-based crypto trading system on **Binance** that operates on the **lowest practical timeframe** using **WebSocket market data**, executes via **Binance API**, and enforces strict risk controls (zero human intervention during runtime).

---

## 0) Scope and Success Criteria

### Objectives
- Ultra-low-latency **event-driven** trading (no polling for market data).
- Strategy based on **Binance-only** signals (microstructure / burst / mean reversion), not external “oracle-lag” arbitrage.
- Production-grade reliability: reconnects, backpressure, idempotency, kill switches.

### What “success” means (measurable)
- End-to-end latency (WS tick → decision → order sent) p50 < 50–150ms on a normal VPS.
- Deterministic behavior: same inputs yield same actions.
- Robustness: 24h run without manual restart under normal network conditions.
- Risk outcomes: daily drawdown capped (hard stop) + no runaway order spam.

### Non-goals (explicit)
- No claims of guaranteed profit.
- No HFT/colocation assumptions.
- No “stale feed arbitrage” since reference and execution are Binance.

---

## 1) Platform Choice and Market Selection

### Binance Segment
- **Primary recommendation:** USD-M Futures (deep liquidity, symmetric long/short, tight spreads)
- Alternative: Spot (simpler risk; may be slightly less “microstructure-friendly” for shorting)

### Instruments (start small)
- Phase 1: `BTCUSDT`, `ETHUSDT`
- Phase 2: add `SOLUSDT` only after stability is proven

### Account Modes / Constraints
- Choose:
  - Futures: isolated margin, low leverage initially (e.g., 1–2x for safety)
  - Spot: cash-only initially
- Establish max position size and max exposure per symbol.

---

## 2) Data Feeds (Minimum Timeframe)

### Market Data (WebSocket only)
Subscribe per symbol to:
- `@bookTicker` — best bid/ask changes (lightweight + fast)
- `@aggTrade` — trade prints (aggressive flow proxy)
Optional:
- Futures `@markPrice@1s` — regime sanity checks / liquidation risk guard
- `@depth` (shallow, e.g., `@depth5@100ms`) only if needed (heavier)

### Account/Execution Feeds (strongly recommended)
- User data stream (listenKey) to receive:
  - order updates
  - fills / partials
  - position changes

### Data Normalization Layer
- Normalize all inbound messages into internal structs:
  - timestamps (exchange + local receive time)
  - symbol
  - best bid/ask, spread, mid
  - last trade direction proxy (if derivable), trade size
- Maintain a local monotonic “engine time” clock.

---

## 3) Strategy Design (Binance-only, Microstructure-Based)

### Strategy A — Burst Momentum Catch (recommended first)
**Intent:** Enter with sudden aggressive flow; exit quickly.
- Inputs:
  - `aggTrade` intensity in rolling window (e.g., 250ms–1000ms)
  - mid-price velocity (bps/ms)
  - spread compression / widening
- Trigger example:
  - trades_in_window ≥ threshold AND |mid_move_bps| ≥ threshold
- Entry:
  - aggressive limit or market (depends on slippage controls)
- Exit:
  - time-based (200–800ms) OR
  - take-profit (tiny) OR
  - stop-loss (tight) OR
  - if flow stalls/reverses

### Strategy B — Post-Sweep Micro Mean Reversion (phase 2)
**Intent:** After a spike, fade small when liquidity refills.
- Trigger:
  - sharp move + sudden drop in trade intensity + spread normalizes
- Entry: small contrarian
- Exit: quick mean reversion; strict stop

### Strategy C — “Market-make lite” (phase 3)
**Intent:** Quote both sides; capture spread; manage inventory.
- High complexity: adverse selection, cancel/replace, inventory skew.
- Only after robust infra + measurements.

---

## 4) Execution Model (Orders, Fills, and Slippage)

### Order Types (default)
- Start with:
  - Limit IOC/FOK if supported (or aggressive limit)
  - Market only if you accept slippage risk
- Prefer aggressive limit near best bid/ask to reduce tail slippage.

### Execution Rules
- One active position per symbol initially.
- Cooldown between trades (e.g., 500ms–5s) to prevent churn.
- Max orders per minute per symbol.

### Partial Fill Handling
- If entry partially fills:
  - either cancel remainder and manage the partial as a valid position
  - or continue until a min_fill_qty is reached
- For exits:
  - if partial exit occurs, chase only within defined slippage bounds.

### Idempotency / Duplicate Protection
- Client order IDs must be unique and deterministic.
- All order actions must be safe to retry.

---

## 5) Risk Management (Hard Kill Switches)

### Hard stops (must-have)
- Daily loss limit (absolute USD or % equity)
- Max drawdown from peak
- Max consecutive stopouts
- Max position size per symbol
- Max total exposure

### Latency / Data Integrity kills
Stop trading if:
- WS event lag > threshold (e.g., > 250–500ms sustained)
- reconnect storms (N reconnects within M minutes)
- inbound queue backlog exceeds safe depth
- local clock drift / timestamp errors cause request rejections

### Market regime kills
Stop trading if:
- spread widens above threshold
- volatility spikes beyond calibrated band
- mark price anomaly / index dislocation (futures)

### Safety valves
- “Flatten all” routine
- Cancel all open orders
- Disable trading and keep monitoring until manual reset

---

## 6) System Architecture (Production Shape)

### Services / Modules
1. **MarketData Ingest**
   - WebSocket client(s)
   - reconnect + backoff
   - normalization
2. **State Store**
   - top-of-book, last trades, rolling metrics
   - ring buffers for windows (250ms / 1s / 5s)
3. **Strategy Engine**
   - stateless decision function reading current state
   - emits intents (EnterLong/EnterShort/Exit/Flatten)
4. **Execution Engine**
   - transforms intents into order actions
   - rate-limits actions
   - tracks orders, fills, open positions
5. **Risk Engine**
   - independent watchdog
   - can veto strategy intents
   - can force flatten/stop
6. **Persistence & Telemetry**
   - structured logs
   - metrics (latency, fills, slippage, win rate)
   - optional time-series DB
7. **Supervisor**
   - process health
   - auto-restart policy
   - config hot-reload (optional, careful!)

### Performance principles
- No blocking in the hot path.
- Use bounded queues and drop/slow policies.
- Keep indicators minimal; compute rolling stats incrementally.

---

## 7) Tech Stack Recommendation

### Language
- **Rust** (best for latency + predictability) OR
- Go (simple concurrency) OR
- Python only if you accept higher tail latency (can still work for seconds-level)

### Core Libraries (Rust idea)
- `tokio` for async runtime
- `tokio-tungstenite` for WebSockets
- `reqwest` for REST
- `serde` for JSON
- `tracing` for structured logs
- HMAC signing for Binance REST

---

## 8) Testing Plan (Don’t Skip)

### Unit Tests
- message parsers
- rolling window calculations
- trigger logic correctness
- risk veto logic

### Integration Tests
- WS reconnect simulation
- REST failure / retry simulation
- idempotent order submission

### Paper / Sandbox
- Run in “observe-only mode” first:
  - log all triggers without trading
  - compute hypothetical PnL using conservative slippage assumptions

### Live Small
- smallest position size
- high cooldown
- strict daily loss limit
- monitor fills vs expectations

---

## 9) Measurement & Calibration

### What to log per trade
- timestamp decision + timestamp order sent + timestamp fill
- mid at decision, best bid/ask
- spread
- trade intensity metric
- slippage (fill vs expected)
- hold time
- exit reason

### Calibrate thresholds
- Use recorded WS data to compute:
  - distribution of spread
  - distribution of short-window mid moves
  - frequency of bursts
- Pick triggers that fire rarely at first; expand later.

---

## 10) Milestones (Build Order)

### Milestone 1 — Market Data Recorder (2–4 sessions)
- Connect to WS streams
- Normalize messages
- Persist to disk (compressed)
- Basic latency metrics + dashboards

**Output:** reproducible dataset of live ticks + lag stats

### Milestone 2 — Strategy Simulator (offline)
- Replay recorded ticks
- Implement Strategy A logic
- Include conservative fill model and fees
- Produce summary report

**Output:** evidence the strategy is not purely noise under conservative assumptions

### Milestone 3 — Paper Execution Mode (dry-run)
- Strategy emits intents
- Execution engine logs “would place order”
- Risk engine fully enforced

**Output:** stable runtime + correct throttling + no runaway

### Milestone 4 — Live Small (real orders)
- One symbol only (BTCUSDT)
- Strict limits and cooldown
- User data stream confirms fills

**Output:** validated real slippage distribution and failure modes

### Milestone 5 — Scale Carefully
- Add ETHUSDT
- Reduce cooldown
- Increase size only if slippage remains bounded and edge remains positive

**Output:** stable profitability after fees in multiple regimes (or stop)

---

## 11) Deployment Plan

### Environment
- VPS near Binance infrastructure (region choice matters)
- Stable network + low jitter
- NTP time sync

### Operations
- Config file with:
  - symbols
  - thresholds
  - risk limits
  - max order rate
- Run with:
  - systemd (Linux) or docker compose
- Alerts:
  - Telegram/Slack on kill-switch, reconnect storms, daily stop

---

## 12) Compliance / Safety Notes
- This is a trading system; losses are possible.
- Binance API usage must respect rate limits and platform rules.
- Start with small size and treat this as engineering R&D.

---

## Appendix A — Default Config (Starter Values)

```yaml
mode: observe_then_trade
venue: binance_usdm_futures
symbols: [BTCUSDT, ETHUSDT]

data_streams:
  - bookTicker
  - aggTrade
  - markPrice_1s

strategy:
  type: burst_momentum
  window_ms: 250
  trade_count_trigger: 25
  move_bps_trigger: 8
  entry_qty:
    BTCUSDT: 0.001
    ETHUSDT: 0.01
  exit:
    max_hold_ms: 600
    take_profit_bps: 4
    stop_loss_bps: 6
  cooldown_ms: 1000

risk:
  max_trades_per_min: 60
  daily_loss_usd: 50
  max_consecutive_losses: 3
  max_spread_bps: 6
  max_ws_lag_ms: 300
  reconnect_storm:
    max_reconnects: 5
    window_min: 10



source /Users/celikozdemir/Projects/algo-trading/.venv/bin/activate
source .venv/bin/activate

uvicorn backend.main:app --reload
# Check how much data you have
python -m workers.run_backtest --symbol BTCUSDT --diagnose

# Then run the backtest with current config
python -m workers.run_backtest --symbol BTCUSDT

# Or sweep the full grid
python -m workers.grid_search --symbol BTCUSDT

ssh -i '/Users/celikozdemir/Library/CloudStorage/Dropbox/Personal/Celik Ozdemir/Binance/aws/micro-trading-key.pem' ubuntu@13.114.223.56


# All three services at once
sudo systemctl status algo-api algo-recorder

# Press Ctrl+C first to cancel the hanging restart, then:
sudo systemctl kill -s SIGKILL algo-api
sudo systemctl start algo-api

# Live logs from recorder
journalctl -u algo-recorder -f

# Live logs from API
journalctl -u algo-api -f

# Check Docker containers
docker ps
If you're not using systemd yet (still running manually):


# See what's running on port 8000
ss -tlnp | grep 8000

# All Python processes
ps aux | grep python

# All processes by memory usage
ps aux --sort=-%mem | head -10

sudo systemctl restart algo-api


# Quick check of data range and row counts
docker exec algo_db psql -U algo -d algo_trading -c "
SELECT 
  symbol,
  COUNT(*) as rows,
  MIN(timestamp_exchange) as first_tick,
  MAX(timestamp_exchange) as last_tick
FROM book_ticks GROUP BY symbol
UNION ALL
SELECT 
  symbol,
  COUNT(*) as rows,
  MIN(timestamp_exchange),
  MAX(timestamp_exchange)
FROM agg_trades GROUP BY symbol;"
Once you have a few hours of data that spans a weekday session, run the full grid search with more ticks:



  python -m workers.grid_search \
  --symbol BTCUSDT \
  --start 2026-03-02T14:00:00 \
  --end 2026-03-02T14:30:00 \
  --maker

