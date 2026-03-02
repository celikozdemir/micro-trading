'use client'

import { PaperTradeRow } from '@/lib/api'

function fmtMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function fmtTime(ms: number): string {
  const d = new Date(ms)
  return d.toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    timeZone: 'UTC',
  })
}

function fmtDate(ms: number): string {
  const d = new Date(ms)
  return d.toLocaleDateString('en-GB', {
    month: 'short', day: 'numeric', timeZone: 'UTC',
  })
}

const EXIT_COLORS: Record<string, string> = {
  take_profit: 'text-emerald-400',
  stop_loss:   'text-red-400',
  timeout:     'text-yellow-500',
}

interface Props {
  trades: PaperTradeRow[]
  compact?: boolean
}

export default function TradesTable({ trades, compact }: Props) {
  if (trades.length === 0) {
    return (
      <div className="text-center py-12 text-muted-foreground text-sm">
        No trades yet — waiting for the first spike…
      </div>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs font-mono">
        <thead>
          <tr className="border-b border-border">
            <th className="text-left pb-2 pr-4 text-muted-foreground font-normal">Time (UTC)</th>
            <th className="text-left pb-2 pr-4 text-muted-foreground font-normal">Symbol</th>
            <th className="text-left pb-2 pr-4 text-muted-foreground font-normal">Side</th>
            <th className="text-right pb-2 pr-4 text-muted-foreground font-normal">Entry</th>
            <th className="text-right pb-2 pr-4 text-muted-foreground font-normal">Exit</th>
            <th className="text-right pb-2 pr-4 text-muted-foreground font-normal">Hold</th>
            <th className="text-right pb-2 pr-4 text-muted-foreground font-normal">Gross bps</th>
            <th className="text-right pb-2 text-muted-foreground font-normal">Net USD</th>
            {!compact && <th className="text-left pb-2 pl-4 text-muted-foreground font-normal">Exit</th>}
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => {
            const win = t.net_pnl_usd > 0
            return (
              <tr key={t.id} className="border-b border-border/40 hover:bg-accent/30 transition-colors">
                <td className="py-2 pr-4 text-muted-foreground">
                  <span className="text-muted-foreground/50">{fmtDate(t.entry_time_ms)} </span>
                  {fmtTime(t.entry_time_ms)}
                </td>
                <td className="py-2 pr-4 text-foreground/80">{t.symbol}</td>
                <td className={`py-2 pr-4 font-medium ${t.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}`}>
                  {t.side === 'BUY' ? 'LONG' : 'SHORT'}
                </td>
                <td className="py-2 pr-4 text-right text-foreground/80">
                  {t.entry_price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </td>
                <td className="py-2 pr-4 text-right text-foreground/80">
                  {t.exit_price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </td>
                <td className="py-2 pr-4 text-right text-muted-foreground">{fmtMs(t.hold_ms)}</td>
                <td className={`py-2 pr-4 text-right ${win ? 'text-emerald-400' : 'text-red-400'}`}>
                  {t.gross_pnl_bps >= 0 ? '+' : ''}{t.gross_pnl_bps.toFixed(2)}
                </td>
                <td className={`py-2 text-right font-medium ${win ? 'text-emerald-400' : 'text-red-400'}`}>
                  {t.net_pnl_usd >= 0 ? '+' : ''}${t.net_pnl_usd.toFixed(4)}
                </td>
                {!compact && (
                  <td className={`py-2 pl-4 ${EXIT_COLORS[t.exit_reason] ?? 'text-muted-foreground'}`}>
                    {t.exit_reason.replace('_', ' ')}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
