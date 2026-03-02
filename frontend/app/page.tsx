'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  getServices, getRunnerStatus, startRunner, stopRunner,
  getPaperStats, getPaperTrades, getStats,
  type ServiceStatus, type PaperTradeStats, type PaperTradeRow, type SymbolStats,
} from '@/lib/api'
import ServiceCard from '@/components/service-card'
import PnlSummary from '@/components/pnl-summary'
import TradesTable from '@/components/trades-table'
import DataStats from '@/components/data-stats'

const EMPTY_STATS: PaperTradeStats = { total_trades: 0, wins: 0, win_rate: 0, net_pnl_usd: 0 }

export default function Dashboard() {
  const [services, setServices] = useState<ServiceStatus[]>([])
  const [actionLoading, setActionLoading] = useState(false)
  const [pnlAll, setPnlAll] = useState<PaperTradeStats>(EMPTY_STATS)
  const [pnlToday, setPnlToday] = useState<PaperTradeStats>(EMPTY_STATS)
  const [recentTrades, setRecentTrades] = useState<PaperTradeRow[]>([])
  const [dbStats, setDbStats] = useState<Record<string, SymbolStats>>({})

  const refreshServices = useCallback(async () => {
    try {
      const [svcs, runner] = await Promise.all([getServices(), getRunnerStatus()])
      setServices(svcs.map(s =>
        s.name === 'algo-recorder'
          ? { ...s, active: runner.running, uptime_s: runner.running ? runner.uptime_s : undefined }
          : s
      ))
    } catch { /* keep previous state */ }
  }, [])

  const refreshPnl = useCallback(async () => {
    try {
      const data = await getPaperStats()
      setPnlAll(data.all_time)
      setPnlToday(data.today)
    } catch { /* ignore */ }
  }, [])

  const refreshTrades = useCallback(async () => {
    try {
      const data = await getPaperTrades(undefined, 10)
      setRecentTrades(data.trades)
    } catch { /* ignore */ }
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

  const handleStart = async () => {
    setActionLoading(true)
    try { await startRunner(); await refreshServices() } finally { setActionLoading(false) }
  }

  const handleStop = async () => {
    setActionLoading(true)
    try { await stopRunner(); await refreshServices() } finally { setActionLoading(false) }
  }

  const placeholderServices: ServiceStatus[] = [
    { name: 'algo-recorder', display: 'Data Recorder', active: false },
    { name: 'algo-paper',    display: 'Paper Trader',  active: false },
    { name: 'database',      display: 'Database',      active: false },
  ]

  const displayServices = services.length > 0 ? services : placeholderServices

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-foreground">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-0.5">Live system monitor</p>
      </div>

      {/* Services */}
      <section>
        <h2 className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">Services</h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {displayServices.map(s => (
            <ServiceCard
              key={s.name}
              service={s}
              onStart={s.name === 'algo-recorder' ? handleStart : undefined}
              onStop={s.name === 'algo-recorder' ? handleStop : undefined}
              actionLoading={s.name === 'algo-recorder' ? actionLoading : undefined}
            />
          ))}
        </div>
      </section>

      {/* P&L Summary */}
      <section>
        <h2 className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">Performance</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <PnlSummary label="Today" stats={pnlToday} />
          <PnlSummary label="All Time" stats={pnlAll} />
        </div>
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
          <TradesTable trades={recentTrades} compact />
        </div>

        <div className="bg-card border border-border rounded-xl p-5">
          <h2 className="text-sm font-medium text-foreground mb-4">Recorded Data</h2>
          <DataStats stats={dbStats} />
        </div>
      </section>
    </div>
  )
}
