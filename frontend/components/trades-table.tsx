'use client'

import { useEffect, useState } from 'react'
import { type PaperTradeRow, type LivePosition, type LiveConfig } from '@/lib/api'

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

// Compute SL and TP bps for a live position given strategy config
function computeLevels(pos: LivePosition, cfg: LiveConfig) {
  const trailActive = pos.high_watermark_bps >= cfg.trail_trigger_bps
  const slBps = trailActive
    ? pos.high_watermark_bps - cfg.trail_bps   // trailing: moves up with peak
    : -cfg.stop_loss_bps                        // fixed: always -5
  const tpBps = cfg.take_profit_bps            // always +10
  return { slBps, tpBps, trailActive }
}

// Live row — hold timer ticks every 200ms, P&L + levels from parent
function LiveTradeRow({
  symbol, pos, cfg, compact,
}: {
  symbol: string
  pos: LivePosition
  cfg: LiveConfig
  compact?: boolean
}) {
  const [holdMs, setHoldMs] = useState(Date.now() - pos.entry_time_ms)

  useEffect(() => {
    const t = setInterval(() => setHoldMs(Date.now() - pos.entry_time_ms), 200)
    return () => clearInterval(t)
  }, [pos.entry_time_ms])

  const pnl = pos.current_pnl_bps
  const notional = pos.entry_price * pos.qty
  const netUsd   = notional * pnl / 10000 - notional * 4 / 10000
  const pnlColor = pnl > 0 ? 'text-emerald-400' : pnl < 0 ? 'text-red-400' : 'text-muted-foreground'
  const rowBg    = pos.side === 'BUY' ? 'bg-emerald-500/5' : 'bg-red-500/5'

  const { slBps, tpBps, trailActive } = computeLevels(pos, cfg)
  // SL color: yellow when trailing (profitable stop), red when at fixed floor
  const slColor = trailActive ? 'text-yellow-400' : 'text-red-400/70'

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
      <td className={`py-2 pr-4 text-right ${pnlColor} opacity-70`}>
        ~{netUsd >= 0 ? '+' : ''}${netUsd.toFixed(4)}
      </td>
      {/* SL column — red when fixed floor, yellow when trailing has locked in profit */}
      <td className={`py-2 pr-4 text-right font-medium tabular-nums ${slColor}`}>
        {slBps >= 0 ? '+' : ''}{slBps.toFixed(1)}
        {trailActive && (
          <span className="ml-1 text-muted-foreground/40 text-[10px]">↑</span>
        )}
      </td>
      {/* TP column — always fixed emerald */}
      <td className="py-2 text-right font-medium tabular-nums text-emerald-400/70">
        +{tpBps.toFixed(1)}
      </td>
    </tr>
  )
}

export interface LivePositionEntry {
  symbol: string
  pos: LivePosition
  cfg: LiveConfig
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
            <th className="text-right pb-2 pr-4 text-muted-foreground font-normal">Net USD</th>
            <th className="text-right pb-2 pr-4 text-red-400/50 font-normal">SL bps</th>
            <th className="text-right pb-2 text-emerald-400/50 font-normal">TP bps</th>
          </tr>
        </thead>
        <tbody>
          {/* Live open positions pinned to the top */}
          {livePositions?.map(({ symbol, pos, cfg }) => (
            <LiveTradeRow key={`live-${symbol}`} symbol={symbol} pos={pos} cfg={cfg} compact={compact} />
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
                <td className={`py-2 pr-4 text-right font-medium ${win ? 'text-emerald-400' : 'text-red-400'}`}>
                  {t.net_pnl_usd >= 0 ? '+' : ''}${t.net_pnl_usd.toFixed(4)}
                </td>
                {/* SL/TP — dashes for closed trades (outcome already known) */}
                <td className="py-2 pr-4 text-right text-muted-foreground/30">—</td>
                <td className="py-2 text-right text-muted-foreground/30">—</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
