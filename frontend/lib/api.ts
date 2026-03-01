const BASE = 'http://localhost:8000/api'

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}`)
  return res.json()
}

// Runner
export const getRunnerStatus = () => req<RunnerStatus>('GET', '/runner/status')
export const startRunner = () => req<{ ok: boolean }>('POST', '/runner/start')
export const stopRunner = () => req<{ ok: boolean }>('POST', '/runner/stop')

// Stats
export const getStats = () => req<Record<string, SymbolStats>>('GET', '/stats')

// Config
export const getConfig = () => req<TradingConfig>('GET', '/config')
export const updateConfig = (updates: Partial<TradingConfig>) =>
  req<TradingConfig>('PUT', '/config', updates)

// Backtest
export const runBacktest = (symbol: string, start?: string, end?: string) =>
  req<BacktestResult>('POST', '/backtest', { symbol, start, end })

// Types
export interface RunnerStatus {
  running: boolean
  uptime_s: number
  total_book_ticks: number
  total_agg_trades: number
  buffer_book_ticks: number
  buffer_agg_trades: number
}

export interface SymbolStats {
  agg_trades: number
  book_ticks: number
  earliest: string | null
  latest: string | null
}

export interface TradingConfig {
  mode: string
  venue: string
  symbols: string[]
  strategy: {
    window_ms: number
    trade_count_trigger: number
    move_bps_trigger: number
    cooldown_ms: number
    entry_qty: Record<string, number>
    exit: {
      take_profit_bps: number
      stop_loss_bps: number
      max_hold_ms: number
    }
  }
  risk: {
    daily_loss_usd: number
    max_spread_bps: number
    max_ws_lag_ms: number
    max_trades_per_min: number
    max_consecutive_losses: number
    reconnect_storm: { max_reconnects: number; window_min: number }
  }
}

export interface BacktestResult {
  total_trades: number
  wins?: number
  losses?: number
  win_rate?: number
  avg_hold_ms?: number
  avg_gross_bps?: number
  total_fees_usd?: number
  net_pnl_usd?: number
  max_drawdown_usd?: number
  exit_reasons?: Record<string, number>
  message?: string
  trades?: TradeRecord[]
}

export interface TradeRecord {
  side: string
  entry_price: number
  exit_price: number
  qty: number
  hold_ms: number
  exit_reason: string
  net_pnl_usd: number
  gross_pnl_bps: number
}
