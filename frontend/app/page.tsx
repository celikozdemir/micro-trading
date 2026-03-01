'use client'

import { useEffect, useState, useCallback } from 'react'
import {
  getRunnerStatus,
  startRunner,
  stopRunner,
  getStats,
  getConfig,
  updateConfig,
  runBacktest,
  type RunnerStatus,
  type SymbolStats,
  type TradingConfig,
  type BacktestResult,
} from '@/lib/api'

// ── helpers ────────────────────────────────────────────────────────────────

function fmt(n: number | undefined | null, decimals = 0) {
  if (n == null) return '—'
  return n.toLocaleString(undefined, { maximumFractionDigits: decimals })
}

function fmtTs(ts: string | null) {
  if (!ts) return '—'
  return new Date(ts).toLocaleString()
}

function Badge({ running }: { running: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium ${
        running ? 'bg-emerald-500/20 text-emerald-400' : 'bg-gray-700 text-gray-400'
      }`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${running ? 'bg-emerald-400 animate-pulse' : 'bg-gray-500'}`} />
      {running ? 'Running' : 'Stopped'}
    </span>
  )
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">{title}</h2>
      {children}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-gray-500">{label}</span>
      <span className="text-sm font-mono text-gray-200">{value}</span>
    </div>
  )
}

// ── Recorder Card ───────────────────────────────────────────────────────────

function RecorderCard() {
  const [status, setStatus] = useState<RunnerStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchStatus = useCallback(async () => {
    try {
      const s = await getRunnerStatus()
      setStatus(s)
      setError(null)
    } catch {
      setError('API unreachable')
    }
  }, [])

  useEffect(() => {
    fetchStatus()
    const id = setInterval(fetchStatus, 3000)
    return () => clearInterval(id)
  }, [fetchStatus])

  async function toggle() {
    setLoading(true)
    try {
      if (status?.running) {
        await stopRunner()
      } else {
        await startRunner()
      }
      await fetchStatus()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally {
      setLoading(false)
    }
  }

  const running = status?.running ?? false

  return (
    <Card title="Data Recorder">
      <div className="flex items-center justify-between mb-4">
        <Badge running={running} />
        <button
          onClick={toggle}
          disabled={loading}
          className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 ${
            running
              ? 'bg-red-600/20 text-red-400 hover:bg-red-600/30 border border-red-700/40'
              : 'bg-emerald-600/20 text-emerald-400 hover:bg-emerald-600/30 border border-emerald-700/40'
          }`}
        >
          {loading ? '…' : running ? 'Stop' : 'Start'}
        </button>
      </div>

      {error && <p className="text-xs text-red-400 mb-3">{error}</p>}

      <div className="grid grid-cols-2 gap-3">
        <Stat label="Uptime" value={status ? `${Math.floor(status.uptime_s)}s` : '—'} />
        <Stat label="Book ticks (total)" value={fmt(status?.total_book_ticks)} />
        <Stat label="Agg trades (total)" value={fmt(status?.total_agg_trades)} />
        <Stat label="Buffer (book)" value={fmt(status?.buffer_book_ticks)} />
        <Stat label="Buffer (trades)" value={fmt(status?.buffer_agg_trades)} />
      </div>
    </Card>
  )
}

// ── Stats Card ──────────────────────────────────────────────────────────────

function StatsCard() {
  const [stats, setStats] = useState<Record<string, SymbolStats>>({})

  useEffect(() => {
    async function fetch_() {
      try { setStats(await getStats()) } catch { /* ignore */ }
    }
    fetch_()
    const id = setInterval(fetch_, 5000)
    return () => clearInterval(id)
  }, [])

  const symbols = Object.keys(stats)

  return (
    <Card title="DB Stats">
      {symbols.length === 0 ? (
        <p className="text-xs text-gray-600">No data yet</p>
      ) : (
        <div className="space-y-4">
          {symbols.map((sym) => (
            <div key={sym}>
              <p className="text-xs font-semibold text-gray-300 mb-2">{sym}</p>
              <div className="grid grid-cols-2 gap-2">
                <Stat label="Agg trades" value={fmt(stats[sym].agg_trades)} />
                <Stat label="Book ticks" value={fmt(stats[sym].book_ticks)} />
                <Stat label="Earliest" value={fmtTs(stats[sym].earliest)} />
                <Stat label="Latest" value={fmtTs(stats[sym].latest)} />
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

// ── Backtest Card ───────────────────────────────────────────────────────────

function BacktestCard({ symbols }: { symbols: string[] }) {
  const [symbol, setSymbol] = useState(symbols[0] ?? 'BTCUSDT')
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run() {
    setRunning(true)
    setError(null)
    setResult(null)
    try {
      const r = await runBacktest(symbol)
      setResult(r)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed')
    } finally {
      setRunning(false)
    }
  }

  return (
    <Card title="Backtest">
      <div className="flex gap-2 mb-4">
        <select
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-gray-500"
        >
          {symbols.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <button
          onClick={run}
          disabled={running}
          className="px-4 py-1.5 rounded-lg text-sm font-medium bg-blue-600/20 text-blue-400 hover:bg-blue-600/30 border border-blue-700/40 disabled:opacity-50 transition-colors"
        >
          {running ? 'Running…' : 'Run'}
        </button>
      </div>

      {error && <p className="text-xs text-red-400 mb-3">{error}</p>}

      {result && (
        <div className="space-y-3">
          {result.message && (
            <p className="text-xs text-yellow-400">{result.message}</p>
          )}
          <div className="grid grid-cols-2 gap-2">
            <Stat label="Total trades" value={fmt(result.total_trades)} />
            <Stat label="Win rate" value={result.win_rate != null ? `${(result.win_rate * 100).toFixed(1)}%` : '—'} />
            <Stat label="Net PnL" value={result.net_pnl_usd != null ? `$${result.net_pnl_usd.toFixed(2)}` : '—'} />
            <Stat label="Max drawdown" value={result.max_drawdown_usd != null ? `$${result.max_drawdown_usd.toFixed(2)}` : '—'} />
            <Stat label="Avg hold" value={result.avg_hold_ms != null ? `${result.avg_hold_ms.toFixed(0)}ms` : '—'} />
            <Stat label="Avg gross" value={result.avg_gross_bps != null ? `${result.avg_gross_bps.toFixed(2)} bps` : '—'} />
            <Stat label="Total fees" value={result.total_fees_usd != null ? `$${result.total_fees_usd.toFixed(4)}` : '—'} />
          </div>

          {result.exit_reasons && (
            <div>
              <p className="text-xs text-gray-500 mb-1">Exit reasons</p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(result.exit_reasons).map(([k, v]) => (
                  <span key={k} className="text-xs bg-gray-800 rounded px-2 py-0.5 text-gray-300">
                    {k}: {v}
                  </span>
                ))}
              </div>
            </div>
          )}

          {result.trades && result.trades.length > 0 && (
            <div>
              <p className="text-xs text-gray-500 mb-1">Trades</p>
              <div className="overflow-x-auto">
                <table className="w-full text-xs font-mono">
                  <thead>
                    <tr className="text-gray-500 border-b border-gray-800">
                      <th className="text-left pb-1 pr-3">Side</th>
                      <th className="text-right pb-1 pr-3">Entry</th>
                      <th className="text-right pb-1 pr-3">Exit</th>
                      <th className="text-right pb-1 pr-3">Hold(ms)</th>
                      <th className="text-right pb-1 pr-3">PnL</th>
                      <th className="text-left pb-1">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.map((t, i) => (
                      <tr key={i} className="border-b border-gray-800/50">
                        <td className={`py-1 pr-3 ${t.side === 'long' ? 'text-emerald-400' : 'text-red-400'}`}>{t.side}</td>
                        <td className="py-1 pr-3 text-right text-gray-300">{t.entry_price.toFixed(2)}</td>
                        <td className="py-1 pr-3 text-right text-gray-300">{t.exit_price.toFixed(2)}</td>
                        <td className="py-1 pr-3 text-right text-gray-400">{t.hold_ms.toFixed(0)}</td>
                        <td className={`py-1 pr-3 text-right ${t.net_pnl_usd >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          ${t.net_pnl_usd.toFixed(4)}
                        </td>
                        <td className="py-1 text-gray-500">{t.exit_reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

// ── Config Card ─────────────────────────────────────────────────────────────

type StrategyDraft = {
  window_ms: number
  trade_count_trigger: number
  move_bps_trigger: number
  cooldown_ms: number
  take_profit_bps: number
  stop_loss_bps: number
  max_hold_ms: number
}

type RiskDraft = {
  daily_loss_usd: number
  max_spread_bps: number
  max_ws_lag_ms: number
  max_trades_per_min: number
  max_consecutive_losses: number
}

function ConfigCard({ onSymbolsChange }: { onSymbolsChange: (s: string[]) => void }) {
  const [config, setConfig] = useState<TradingConfig | null>(null)
  const [strategy, setStrategy] = useState<StrategyDraft | null>(null)
  const [risk, setRisk] = useState<RiskDraft | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getConfig().then((c) => {
      setConfig(c)
      onSymbolsChange(c.symbols)
      setStrategy({
        window_ms: c.strategy.window_ms,
        trade_count_trigger: c.strategy.trade_count_trigger,
        move_bps_trigger: c.strategy.move_bps_trigger,
        cooldown_ms: c.strategy.cooldown_ms,
        take_profit_bps: c.strategy.exit.take_profit_bps,
        stop_loss_bps: c.strategy.exit.stop_loss_bps,
        max_hold_ms: c.strategy.exit.max_hold_ms,
      })
      setRisk({
        daily_loss_usd: c.risk.daily_loss_usd,
        max_spread_bps: c.risk.max_spread_bps,
        max_ws_lag_ms: c.risk.max_ws_lag_ms,
        max_trades_per_min: c.risk.max_trades_per_min,
        max_consecutive_losses: c.risk.max_consecutive_losses,
      })
    }).catch(() => setError('Failed to load config'))
  }, [onSymbolsChange])

  function numInput(
    label: string,
    value: number,
    onChange: (v: number) => void,
    step = 1,
    min?: number
  ) {
    return (
      <label key={label} className="flex flex-col gap-1">
        <span className="text-xs text-gray-500">{label}</span>
        <input
          type="number"
          value={value}
          step={step}
          min={min}
          onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
          className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-gray-500 w-full"
        />
      </label>
    )
  }

  async function save() {
    if (!strategy || !risk || !config) return
    setSaving(true)
    setError(null)
    try {
      await updateConfig({
        strategy: {
          ...config.strategy,
          window_ms: strategy.window_ms,
          trade_count_trigger: strategy.trade_count_trigger,
          move_bps_trigger: strategy.move_bps_trigger,
          cooldown_ms: strategy.cooldown_ms,
          exit: {
            take_profit_bps: strategy.take_profit_bps,
            stop_loss_bps: strategy.stop_loss_bps,
            max_hold_ms: strategy.max_hold_ms,
          },
        },
        risk: {
          ...config.risk,
          daily_loss_usd: risk.daily_loss_usd,
          max_spread_bps: risk.max_spread_bps,
          max_ws_lag_ms: risk.max_ws_lag_ms,
          max_trades_per_min: risk.max_trades_per_min,
          max_consecutive_losses: risk.max_consecutive_losses,
        },
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  if (!strategy || !risk) {
    return (
      <Card title="Config">
        <p className="text-xs text-gray-600">{error ?? 'Loading…'}</p>
      </Card>
    )
  }

  const setS = (k: keyof StrategyDraft) => (v: number) => setStrategy((p) => p ? { ...p, [k]: v } : p)
  const setR = (k: keyof RiskDraft) => (v: number) => setRisk((p) => p ? { ...p, [k]: v } : p)

  return (
    <Card title="Config">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-4">
        <div className="col-span-2 md:col-span-4">
          <p className="text-xs font-semibold text-gray-400 mb-3 uppercase tracking-wider">Strategy</p>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {numInput('Window (ms)', strategy.window_ms, setS('window_ms'), 10, 50)}
            {numInput('Trade count trigger', strategy.trade_count_trigger, setS('trade_count_trigger'), 1, 1)}
            {numInput('Move trigger (bps)', strategy.move_bps_trigger, setS('move_bps_trigger'), 0.1, 0)}
            {numInput('Cooldown (ms)', strategy.cooldown_ms, setS('cooldown_ms'), 100, 0)}
            {numInput('Take profit (bps)', strategy.take_profit_bps, setS('take_profit_bps'), 0.5, 0)}
            {numInput('Stop loss (bps)', strategy.stop_loss_bps, setS('stop_loss_bps'), 0.5, 0)}
            {numInput('Max hold (ms)', strategy.max_hold_ms, setS('max_hold_ms'), 100, 100)}
          </div>
        </div>

        <div className="col-span-2 md:col-span-4">
          <p className="text-xs font-semibold text-gray-400 mb-3 uppercase tracking-wider">Risk</p>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {numInput('Daily loss limit ($)', risk.daily_loss_usd, setR('daily_loss_usd'), 5, 0)}
            {numInput('Max spread (bps)', risk.max_spread_bps, setR('max_spread_bps'), 0.5, 0)}
            {numInput('Max WS lag (ms)', risk.max_ws_lag_ms, setR('max_ws_lag_ms'), 50, 50)}
            {numInput('Max trades/min', risk.max_trades_per_min, setR('max_trades_per_min'), 1, 1)}
            {numInput('Max consec. losses', risk.max_consecutive_losses, setR('max_consecutive_losses'), 1, 1)}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-3 mt-5 pt-4 border-t border-gray-800">
        <button
          onClick={save}
          disabled={saving}
          className="px-5 py-1.5 rounded-lg text-sm font-medium bg-indigo-600/20 text-indigo-400 hover:bg-indigo-600/30 border border-indigo-700/40 disabled:opacity-50 transition-colors"
        >
          {saving ? 'Saving…' : 'Save config'}
        </button>
        {saved && <span className="text-xs text-emerald-400">Saved ✓</span>}
        {error && <span className="text-xs text-red-400">{error}</span>}
        <span className="text-xs text-gray-600 ml-auto">Restart runner to apply strategy changes</span>
      </div>
    </Card>
  )
}

// ── Page ────────────────────────────────────────────────────────────────────

export default function Page() {
  const [symbols, setSymbols] = useState<string[]>(['BTCUSDT', 'ETHUSDT'])

  return (
    <main className="max-w-7xl mx-auto px-4 py-8 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-gray-100 tracking-tight">Algo Trading Platform</h1>
        <span className="text-xs text-gray-600 font-mono">Binance USDM Microstructure</span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <RecorderCard />
        <StatsCard />
        <BacktestCard symbols={symbols} />
      </div>

      <ConfigCard onSymbolsChange={setSymbols} />
    </main>
  )
}
