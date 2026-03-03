'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  getPaperTrades, getPaperStats, clearPaperTrades, getLiveState,
  type PaperTradeRow, type PaperTradeStats, type LiveState,
} from '@/lib/api'
import PnlSummary from '@/components/pnl-summary'
import TradesTable, { type LivePositionEntry } from '@/components/trades-table'

const SYMBOLS = ['', 'BTCUSDT', 'ETHUSDT'] as const
const PAGE_SIZE = 50
const EMPTY_STATS: PaperTradeStats = { total_trades: 0, wins: 0, win_rate: 0, net_pnl_usd: 0 }

function livePositionsFrom(state: LiveState | null): LivePositionEntry[] {
  if (!state) return []
  const cfg = state.config ?? { take_profit_bps: 10, stop_loss_bps: 5, trail_trigger_bps: 4, trail_bps: 2 }
  return Object.entries(state.positions)
    .filter(([, pos]) => pos != null)
    .map(([symbol, pos]) => ({ symbol, pos: pos!, cfg }))
}

export default function TradesPage() {
  const [symbol, setSymbol]       = useState('')
  const [page, setPage]           = useState(0)
  const [trades, setTrades]       = useState<PaperTradeRow[]>([])
  const [total, setTotal]         = useState(0)
  const [stats, setStats]         = useState<{ all_time: PaperTradeStats; today: PaperTradeStats }>({
    all_time: EMPTY_STATS,
    today: EMPTY_STATS,
  })
  const [confirming, setConfirming] = useState(false)
  const [clearing, setClearing]     = useState(false)
  const [liveState, setLiveState]   = useState<LiveState | null>(null)

  const load = useCallback(async () => {
    try {
      const [tradesData, statsData] = await Promise.all([
        getPaperTrades(symbol || undefined, PAGE_SIZE, page * PAGE_SIZE),
        getPaperStats(symbol || undefined),
      ])
      setTrades(tradesData.trades)
      setTotal(tradesData.total)
      setStats(statsData)
    } catch { /* ignore */ }
  }, [symbol, page])

  const refreshLive = useCallback(async () => {
    try { setLiveState(await getLiveState()) } catch { /* keep previous */ }
  }, [])

  useEffect(() => {
    load()
    refreshLive()
    const t1 = setInterval(load, 10_000)
    const t2 = setInterval(refreshLive, 500)
    return () => { clearInterval(t1); clearInterval(t2) }
  }, [load, refreshLive])

  const handleClear = async () => {
    if (!confirming) { setConfirming(true); return }
    setClearing(true)
    try {
      await clearPaperTrades()
      setPage(0)
      await load()
    } finally {
      setClearing(false)
      setConfirming(false)
    }
  }

  // Reset to page 0 when filter changes
  useEffect(() => { setPage(0) }, [symbol])

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const livePosEntries = livePositionsFrom(liveState)

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-foreground">Paper Trades</h1>
        <p className="text-sm text-muted-foreground mt-0.5">Live strategy decisions — no real orders placed</p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <PnlSummary label="Today" stats={stats.today} />
        <PnlSummary label="All Time" stats={stats.all_time} />
      </div>

      {/* Filter + pagination controls */}
      <div className="flex items-center gap-3">
        <div className="flex rounded-lg overflow-hidden border border-border">
          {SYMBOLS.map(s => (
            <button
              key={s}
              onClick={() => setSymbol(s)}
              className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                symbol === s
                  ? 'bg-accent text-accent-foreground'
                  : 'bg-card text-muted-foreground hover:text-foreground'
              }`}
            >
              {s || 'All'}
            </button>
          ))}
        </div>

        <span className="text-xs text-muted-foreground/50 ml-auto">
          {total.toLocaleString()} total trades
        </span>

        {confirming ? (
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-muted-foreground">Delete all?</span>
            <button
              onClick={handleClear}
              disabled={clearing}
              className="px-2.5 py-1.5 rounded-lg text-xs font-medium bg-red-500/15 text-red-400 hover:bg-red-500/25 border border-red-500/20 transition-colors disabled:opacity-40"
            >
              {clearing ? 'Clearing…' : 'Yes, clear'}
            </button>
            <button
              onClick={() => setConfirming(false)}
              className="px-2.5 py-1.5 rounded-lg text-xs text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            onClick={handleClear}
            className="px-2.5 py-1.5 rounded-lg text-xs font-medium text-red-400 bg-red-500/10 border border-red-500/20 hover:bg-red-500/20 transition-colors"
          >
            Clear trades
          </button>
        )}

        <div className="flex items-center gap-1">
          <button
            onClick={() => setPage(p => Math.max(0, p - 1))}
            disabled={page === 0}
            className="px-2.5 py-1.5 rounded-lg text-xs text-muted-foreground hover:text-foreground hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            ← Prev
          </button>
          <span className="text-xs text-muted-foreground/50 px-1">
            {page + 1} / {totalPages}
          </span>
          <button
            onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
            disabled={page >= totalPages - 1}
            className="px-2.5 py-1.5 rounded-lg text-xs text-muted-foreground hover:text-foreground hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            Next →
          </button>
        </div>
      </div>

      {/* Table */}
      <div className="bg-card border border-border rounded-xl p-5">
        <TradesTable trades={trades} livePositions={livePosEntries} />
      </div>
    </div>
  )
}
