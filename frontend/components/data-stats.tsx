'use client'

import { SymbolStats } from '@/lib/api'

interface Props {
  stats: Record<string, SymbolStats>
}

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleDateString('en-GB', { month: 'short', day: 'numeric', timeZone: 'UTC' }) +
    ' ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })
}

export default function DataStats({ stats }: Props) {
  const symbols = Object.keys(stats)

  if (symbols.length === 0) {
    return <p className="text-sm text-muted-foreground/50">No data recorded yet.</p>
  }

  return (
    <div className="space-y-3">
      {symbols.map((symbol) => {
        const s = stats[symbol]
        return (
          <div key={symbol} className="border border-border rounded-lg p-3">
            <p className="text-xs font-medium text-foreground/80 mb-2">{symbol}</p>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs font-mono">
              <div className="flex justify-between">
                <span className="text-muted-foreground/60">Book ticks</span>
                <span className="text-muted-foreground">{s.book_ticks.toLocaleString()}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground/60">Agg trades</span>
                <span className="text-muted-foreground">{s.agg_trades.toLocaleString()}</span>
              </div>
              <div className="col-span-2 flex justify-between pt-1 border-t border-border/60">
                <span className="text-muted-foreground/60">Range</span>
                <span className="text-muted-foreground/70">
                  {fmtDate(s.earliest)} → {fmtDate(s.latest)}
                </span>
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}
