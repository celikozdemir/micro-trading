'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  getServices, controlService,
  getPaperStats, getPaperTrades, getStats,
  type ServiceStatus, type PaperTradeStats, type PaperTradeRow, type SymbolStats, type LiveState,
} from '@/lib/api'
import { useLiveWs } from '@/lib/use-live-ws'
import ServiceCard from '@/components/service-card'
import PnlSummary from '@/components/pnl-summary'
import TradesTable, { type LivePositionEntry } from '@/components/trades-table'
import DataStats from '@/components/data-stats'
import LiveMarketCard from '@/components/live-market-card'
import EquityCurve from '@/components/equity-curve'
import { TradeBreakdownCard, HourlyPerformanceCard } from '@/components/trade-analytics'

const EMPTY_STATS: PaperTradeStats = { total_trades: 0, wins: 0, win_rate: 0, net_pnl_usd: 0 }

const PLACEHOLDER: ServiceStatus[] = [
  { name: 'algo-recorder', display: 'Data Recorder', active: false },
  { name: 'algo-paper',    display: 'Paper Trader',  active: false },
  { name: 'database',      display: 'Database',      active: false },
]

const DEFAULT_CFG = { take_profit_bps: 10, stop_loss_bps: 5, trail_trigger_bps: 4, trail_bps: 2 }

function livePositionsFrom(state: LiveState | null): LivePositionEntry[] {
  if (!state) return []
  const cfg = state.config ?? DEFAULT_CFG
  return Object.entries(state.positions)
    .filter(([, pos]) => pos != null)
    .map(([symbol, pos]) => ({ symbol, pos: pos!, cfg }))
}

export default function Dashboard() {
  const [services, setServices]         = useState<ServiceStatus[]>([])
  const [loadingFor, setLoadingFor]     = useState<string | null>(null)
  const [actionError, setActionError]   = useState<string | null>(null)
  const [pnlAll, setPnlAll]             = useState<PaperTradeStats>(EMPTY_STATS)
  const [pnlToday, setPnlToday]         = useState<PaperTradeStats>(EMPTY_STATS)
  const [recentTrades, setRecentTrades] = useState<PaperTradeRow[]>([])
  const [dbStats, setDbStats]           = useState<Record<string, SymbolStats>>({})

  const { state: liveState } = useLiveWs()

  const refreshServices = useCallback(async () => {
    try { setServices(await getServices()) } catch { /* keep previous */ }
  }, [])

  const refreshPnl = useCallback(async () => {
    try {
      const d = await getPaperStats()
      setPnlAll(d.all_time); setPnlToday(d.today)
    } catch { /* ignore */ }
  }, [])

  const refreshTrades = useCallback(async () => {
    try { setRecentTrades((await getPaperTrades(undefined, 10)).trades) } catch { /* ignore */ }
  }, [])

  const refreshDbStats = useCallback(async () => {
    try { setDbStats(await getStats()) } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    refreshServices(); refreshPnl(); refreshTrades(); refreshDbStats()
    const t1 = setInterval(refreshServices, 5_000)
    const t2 = setInterval(() => { refreshPnl(); refreshTrades() }, 10_000)
    const t3 = setInterval(refreshDbStats, 15_000)
    return () => { clearInterval(t1); clearInterval(t2); clearInterval(t3) }
  }, [refreshServices, refreshPnl, refreshTrades, refreshDbStats])

  const handleAction = async (name: string, action: 'start' | 'stop' | 'restart') => {
    setLoadingFor(name)
    setActionError(null)
    try {
      await controlService(name, action)
      await refreshServices()
    } catch (e) {
      setActionError(e instanceof Error ? e.message : `Failed to ${action} ${name}`)
    } finally {
      setLoadingFor(null)
    }
  }

  const displayServices = services.length > 0 ? services : PLACEHOLDER
  const livePosEntries = livePositionsFrom(liveState)

  return (
    <div className="p-6 w-full space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-foreground">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-0.5">Live system monitor</p>
      </div>

      {/* Services */}
      <section>
        <h2 className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">Services</h2>
        {actionError && (
          <p className="text-xs text-red-400 mb-2 font-mono">{actionError}</p>
        )}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {displayServices.map(s => (
            <ServiceCard
              key={s.name}
              service={s}
              onAction={s.name !== 'database' ? (action) => handleAction(s.name, action) : undefined}
              loading={loadingFor === s.name}
            />
          ))}
        </div>
      </section>

      {/* Live Market */}
      <section>
        <h2 className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">Live Market</h2>
        <LiveMarketCard />
      </section>

      {/* P&L Summary */}
      <section>
        <h2 className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">Performance</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <PnlSummary label="Today" stats={pnlToday} />
          <PnlSummary label="All Time" stats={pnlAll} />
        </div>
      </section>

      {/* Equity Curve */}
      <section>
        <EquityCurve days={7} />
      </section>

      {/* Analytics */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TradeBreakdownCard days={7} />
        <HourlyPerformanceCard days={7} />
      </section>

      {/* Recent Trades + DB Stats */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 bg-card border border-border rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-medium text-foreground">Recent Trades</h2>
            <a href="/trades" className="text-xs text-muted-foreground hover:text-foreground transition-colors">
              View all →
            </a>
          </div>
          <TradesTable trades={recentTrades} compact livePositions={livePosEntries} />
        </div>

        <div className="bg-card border border-border rounded-xl p-5">
          <h2 className="text-sm font-medium text-foreground mb-4">Recorded Data</h2>
          <DataStats stats={dbStats} />
        </div>
      </section>
    </div>
  )
}
