'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { getLiveState, type LiveState, type LivePosition, type LiveSymbol } from '@/lib/api'

function fmtPrice(p: number, symbol: string): string {
  return symbol.startsWith('BTC')
    ? p.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 })
    : p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtHold(entry_time_ms: number): string {
  const ms = Date.now() - entry_time_ms
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`
}

function PriceCell({ symbol, data }: { symbol: string; data: LiveSymbol }) {
  const prevMidRef = useRef<number | null>(null)
  const [flash, setFlash] = useState<'up' | 'down' | null>(null)

  useEffect(() => {
    if (prevMidRef.current !== null && prevMidRef.current !== data.mid) {
      setFlash(data.mid > prevMidRef.current ? 'up' : 'down')
      const t = setTimeout(() => setFlash(null), 400)
      prevMidRef.current = data.mid
      return () => clearTimeout(t)
    }
    prevMidRef.current = data.mid
  }, [data.mid])

  const staleMs = Date.now() - data.ts_ms
  const isStale = staleMs > 3000

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <span className="text-xs font-mono text-muted-foreground">{symbol}</span>
        {isStale && (
          <span className="text-xs text-yellow-500/70 font-mono">stale</span>
        )}
      </div>
      <div
        className={`text-xl font-semibold tabular-nums font-mono transition-colors duration-300 ${
          flash === 'up'
            ? 'text-emerald-400'
            : flash === 'down'
            ? 'text-red-400'
            : 'text-foreground'
        }`}
      >
        ${fmtPrice(data.mid, symbol)}
      </div>
      <div className="flex gap-3 text-xs font-mono text-muted-foreground/70">
        <span>B {fmtPrice(data.bid, symbol)}</span>
        <span>A {fmtPrice(data.ask, symbol)}</span>
        <span className="text-muted-foreground/50">{data.spread_bps.toFixed(2)} bps</span>
      </div>
    </div>
  )
}

function PositionBadge({ symbol, pos, mid }: { symbol: string; pos: LivePosition; mid: number }) {
  const [holdStr, setHoldStr] = useState(fmtHold(pos.entry_time_ms))

  useEffect(() => {
    const t = setInterval(() => setHoldStr(fmtHold(pos.entry_time_ms)), 200)
    return () => clearInterval(t)
  }, [pos.entry_time_ms])

  const pnl = pos.current_pnl_bps
  const pnlColor = pnl > 0 ? 'text-emerald-400' : pnl < 0 ? 'text-red-400' : 'text-muted-foreground'
  const sideColor = pos.side === 'BUY' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-red-500/15 text-red-400'

  // Trail trigger line
  const trailActive = pos.high_watermark_bps >= 4.0
  const trailStop = trailActive ? pos.high_watermark_bps - 2.0 : null

  return (
    <div className="mt-3 border border-border rounded-lg p-3 bg-muted/30 space-y-2">
      <div className="flex items-center gap-2">
        <span className="text-xs font-mono text-muted-foreground">{symbol}</span>
        <span className={`px-2 py-0.5 rounded text-xs font-medium ${sideColor}`}>
          {pos.side === 'BUY' ? '▲ LONG' : '▼ SHORT'}
        </span>
        <span className="ml-auto text-xs font-mono text-muted-foreground">{holdStr}</span>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs font-mono">
        <div className="text-muted-foreground">Entry</div>
        <div className="text-right text-foreground/80">${fmtPrice(pos.entry_price, symbol)}</div>

        <div className="text-muted-foreground">P&L</div>
        <div className={`text-right font-semibold ${pnlColor}`}>
          {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)} bps
        </div>

        {trailActive && trailStop !== null && (
          <>
            <div className="text-muted-foreground">Trail stop</div>
            <div className="text-right text-yellow-400/80">
              +{trailStop.toFixed(2)} bps
              <span className="ml-1 text-muted-foreground/50">(peak {pos.high_watermark_bps.toFixed(2)})</span>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

export default function LiveMarketCard() {
  const [state, setState] = useState<LiveState | null>(null)
  const [lastUpdate, setLastUpdate] = useState<number | null>(null)

  const refresh = useCallback(async () => {
    try {
      const s = await getLiveState()
      setState(s)
      if (s.ts_ms) setLastUpdate(Date.now())
    } catch {
      // keep previous state
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 500)
    return () => clearInterval(t)
  }, [refresh])

  const isLive = lastUpdate !== null && Date.now() - lastUpdate < 3000
  const symbols = state ? Object.keys(state.symbols) : []
  const hasData = symbols.length > 0

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-medium text-foreground">Live Market</h2>
        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {isLive ? (
            <>
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-emerald-400/70">live</span>
            </>
          ) : (
            <>
              <span className="w-1.5 h-1.5 rounded-full bg-muted-foreground/40" />
              <span>offline</span>
            </>
          )}
        </span>
      </div>

      {!hasData ? (
        <p className="text-xs text-muted-foreground font-mono">
          Paper trader not running — start algo-paper to see live prices.
        </p>
      ) : (
        <div className="space-y-4">
          {/* Prices row */}
          <div className="grid grid-cols-2 gap-4">
            {symbols.map(sym => (
              <PriceCell key={sym} symbol={sym} data={state!.symbols[sym]} />
            ))}
          </div>

          {/* Active positions */}
          {symbols.some(sym => state!.positions[sym]) && (
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider mb-1">
                Open Position
              </p>
              {symbols
                .filter(sym => state!.positions[sym] != null)
                .map(sym => (
                  <PositionBadge
                    key={sym}
                    symbol={sym}
                    pos={state!.positions[sym]!}
                    mid={state!.symbols[sym]?.mid ?? 0}
                  />
                ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
