'use client'

import { useEffect, useState } from 'react'
import { type PaperTradeRow, type LivePosition } from '@/lib/api'

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

function fmtPrice(p: number, symbol: string): string {
  return p.toLocaleString(undefined, {
    minimumFractionDigits: symbol.startsWith('BTC') ? 1 : 2,
    maximumFractionDigits: symbol.startsWith('BTC') ? 1 : 2,
  })
}

const EXIT_COLORS: Record<string, string> = {
  take_profit: 'text-emerald-400',
  stop_loss:   'text-red-400',
  timeout:     'text-yellow-500',
}

// Live row — hold timer ticks every 200ms, P&L value from parent
function LiveTradeRow({ symbol, pos, compact }: { symbol: string; pos: LivePosition; compact?: boolean }) {
  const [holdMs, setHoldMs] = useState(Date.now() - pos.entry_time_ms)

  useEffect(() => {
    const t = setInterval(() => setHoldMs(Date.now() - pos.entry_time_ms), 200)
    return () => clearInterval(t)
  }, [pos.entry_time_ms])

  const pnl = pos.current_pnl_bps
  // Estimate net: maker round-trip = 4 bps on entry notional
  const notional = pos.entry_price * pos.qty
  const netUsd   = notional * pnl / 10000 - notional * 4 / 10000
  const pnlColor = pnl > 0 ? 'text-emerald-400' : pnl < 0 ? 'text-red-400' : 'text-muted-foreground'
  const rowBg    = pos.side === 'BUY' ? 'bg-emerald-500/5' : 'bg-red-500/5'

  return (
    <tr className={`border-b border-border/40 ${rowBg}`}>
      <td className="py-2 pr-4 text-muted-foreground">
        <div className="flex items-center gap-1.5">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse flex-shrink-0" />
          <span className="text-muted-foreground/50">{fmtDate(pos.entry_time_ms)} </span>
          {fmtTime(pos.entry_time_ms)}
        </div>
      </td>
      <td className="py-2 pr-4 text-foreground/80">{symbol}</td>
      <td className={`py-2 pr-4 font-medium ${pos.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}`}>
        {pos.side === 'BUY' ? 'LONG' : 'SHORT'}
      </td>
      <td className="py-2 pr-4 text-right text-foreground/80">{fmtPrice(pos.entry_price, symbol)}</td>
      <td className="py-2 pr-4 text-right text-muted-foreground/40 italic">open</td>
      <td className="py-2 pr-4 text-right text-muted-foreground">{fmtMs(holdMs)}</td>
      <td className={`py-2 pr-4 text-right font-medium ${pnlColor}`}>
        {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}
      </td>
      <td className={`py-2 text-right ${pnlColor} opacity-70`}>
        ~{netUsd >= 0 ? '+' : ''}${netUsd.toFixed(4)}
      </td>
      {!compact && (
        <td className="py-2 pl-4">
          <span className="flex items-center gap-1 text-emerald-400/60 italic">
            <span className="w-1 h-1 rounded-full bg-emerald-400 animate-pulse" />
            live
          </span>
        </td>
      )}
    </tr>
  )
}

export interface LivePositionEntry {
  symbol: string
  pos: LivePosition
}

interface Props {
  trades: PaperTradeRow[]
  compact?: boolean
  livePositions?: LivePositionEntry[]
}

export default function TradesTable({ trades, compact, livePositions }: Props) {
  const hasLive = livePositions && livePositions.length > 0

  if (trades.length === 0 && !hasLive) {
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
          {/* Live open positions pinned to the top */}
          {livePositions?.map(({ symbol, pos }) => (
            <LiveTradeRow key={`live-${symbol}`} symbol={symbol} pos={pos} compact={compact} />
          ))}

          {/* Completed trades */}
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
