'use client'

import { PaperTradeStats } from '@/lib/api'

interface Props {
  label: string
  stats: PaperTradeStats
}

function StatBox({ title, value, sub }: { title: string; value: string; sub?: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <p className="text-xs text-muted-foreground">{title}</p>
      <p className="text-lg font-semibold tabular-nums text-foreground">{value}</p>
      {sub && <p className="text-xs text-muted-foreground/60">{sub}</p>}
    </div>
  )
}

export default function PnlSummary({ label, stats }: Props) {
  const pnlColor =
    stats.net_pnl_usd > 0
      ? 'text-emerald-400'
      : stats.net_pnl_usd < 0
      ? 'text-red-400'
      : 'text-muted-foreground'

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <p className="text-xs text-muted-foreground uppercase tracking-wider mb-4">{label}</p>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-5">
        <StatBox
          title="Trades"
          value={String(stats.total_trades)}
          sub={`${stats.wins} wins`}
        />
        <StatBox
          title="Win Rate"
          value={stats.total_trades > 0 ? `${stats.win_rate.toFixed(1)}%` : '—'}
        />
        <div className="flex flex-col gap-0.5">
          <p className="text-xs text-muted-foreground">Net P&L</p>
          <p className={`text-lg font-semibold tabular-nums ${pnlColor}`}>
            {stats.total_trades > 0
              ? `${stats.net_pnl_usd >= 0 ? '+' : ''}$${stats.net_pnl_usd.toFixed(4)}`
              : '—'}
          </p>
        </div>
        <StatBox
          title="Losses"
          value={String(stats.total_trades - stats.wins)}
        />
      </div>
    </div>
  )
}

