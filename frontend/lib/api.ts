const BASE = (process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000') + '/api'

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

// Paper Trades
export const getPaperTrades = (symbol?: string, limit = 50, offset = 0) => {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (symbol) params.set('symbol', symbol)
  return req<{ trades: PaperTradeRow[]; total: number }>('GET', `/paper-trades?${params}`)
}
export const getPaperStats = (symbol?: string) => {
  const params = symbol ? `?symbol=${symbol}` : ''
  return req<{ all_time: PaperTradeStats; today: PaperTradeStats }>('GET', `/paper-trades/stats${params}`)
}
export const clearPaperTrades = () => req<{ deleted: number }>('DELETE', '/paper-trades')

// Services
export const getServices = () => req<ServiceStatus[]>('GET', '/services')
export const controlService = (name: string, action: 'start' | 'stop' | 'restart') =>
  req<{ ok: boolean; active: boolean }>('POST', `/services/${name}/${action}`)

// Live market state
export const getLiveState = () => req<LiveState>('GET', '/live')

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

export interface SymbolOverride {
  obi_threshold?: number
  intensity_spike_mult?: number
  move_bps_trigger?: number
  afi_threshold?: number
  exit?: {
    take_profit_bps?: number
    stop_loss_bps?: number
    max_hold_ms?: number
    trail_trigger_bps?: number
    trail_bps?: number
  }
}

export interface TradingConfig {
  mode: string
  venue: string
  symbols: string[]
  strategy: {
    window_ms: number
    trade_count_trigger: number
    move_bps_trigger: number
    intensity_filter_trades: number
    intensity_filter_window_ms: number
    cooldown_ms: number
    entry_qty: Record<string, number>
    
    // ── Phase 2/3 Burst & Trend Gates ─────────────────────────────
    intensity_spike_mult: number
    afi_threshold: number
    obi_threshold: number
    adaptive_vol_multiplier: number
    short_only: boolean
    
    macro_trend_halflife_ms: number
    macro_trend_warmup_ms: number
    // ───────────────────────────────────────────────────────────────

    symbol_overrides?: Record<string, SymbolOverride>
    
    exit: {
      take_profit_bps: number
      stop_loss_bps: number
      max_hold_ms: number
      trail_trigger_bps?: number
      trail_bps?: number
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

export interface PaperTradeRow {
  id: number
  symbol: string
  side: string
  entry_time_ms: number
  exit_time_ms: number
  entry_price: number
  exit_price: number
  qty: number
  exit_reason: string
  hold_ms: number
  gross_pnl_bps: number
  gross_pnl_usd: number
  fees_usd: number
  net_pnl_usd: number
}

export interface PaperTradeStats {
  total_trades: number
  wins: number
  win_rate: number
  net_pnl_usd: number
}

export interface ServiceStatus {
  name: string
  display: string
  active: boolean
  uptime_s?: number
}

export interface LiveSymbol {
  bid: number
  ask: number
  mid: number
  spread_bps: number
  ts_ms: number
}

export interface LivePosition {
  side: 'BUY' | 'SELL'
  entry_price: number
  entry_time_ms: number
  qty: number
  current_pnl_bps: number
  high_watermark_bps: number
}

export interface LiveConfig {
  take_profit_bps: number
  stop_loss_bps: number
  trail_trigger_bps: number
  trail_bps: number
}

export interface LiveState {
  ts_ms: number | null
  symbols: Record<string, LiveSymbol>
  positions: Record<string, LivePosition | null>
  config?: LiveConfig
}
