'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  ReferenceLine,
} from 'recharts'
import { getEquityCurve, type EquityCurvePoint } from '@/lib/api'

function fmtDate(ms: number): string {
  return new Date(ms).toLocaleDateString('en-GB', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    timeZone: 'UTC',
  })
}

function fmtShortDate(ms: number): string {
  return new Date(ms).toLocaleDateString('en-GB', {
    month: 'short', day: 'numeric', timeZone: 'UTC',
  })
}

interface Props {
  symbol?: string
  days?: number
}

export default function EquityCurve({ symbol, days = 7 }: Props) {
  const [points, setPoints] = useState<EquityCurvePoint[]>([])
  const [summary, setSummary] = useState<{
    total_trades: number; net_pnl_usd: number; max_drawdown_usd: number; peak_pnl_usd: number
  } | null>(null)

  const refresh = useCallback(async () => {
    try {
      const d = await getEquityCurve(symbol, days)
      setPoints(d.points)
      setSummary(d.summary)
    } catch { /* keep previous */ }
  }, [symbol, days])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 30_000)
    return () => clearInterval(t)
  }, [refresh])

  const pnlColor = (summary?.net_pnl_usd ?? 0) >= 0 ? '#34d399' : '#f87171'
  const hasData = points.length > 0

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-sm font-medium text-foreground">Equity Curve</h2>
          {summary && (
            <div className="flex gap-4 mt-1">
              <span className="text-xs text-muted-foreground">
                {summary.total_trades} trades
              </span>
              <span className={`text-xs font-medium ${summary.net_pnl_usd >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {summary.net_pnl_usd >= 0 ? '+' : ''}${summary.net_pnl_usd.toFixed(4)}
              </span>
              <span className="text-xs text-red-400/70">
                DD: ${summary.max_drawdown_usd.toFixed(4)}
              </span>
            </div>
          )}
        </div>
        <span className="text-xs text-muted-foreground">{days}d</span>
      </div>

      {!hasData ? (
        <div className="h-48 flex items-center justify-center text-xs text-muted-foreground font-mono">
          No trade data yet
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <AreaChart data={points} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={pnlColor} stopOpacity={0.3} />
                <stop offset="95%" stopColor={pnlColor} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" strokeOpacity={0.4} />
            <XAxis
              dataKey="ts"
              tickFormatter={fmtShortDate}
              tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              tickFormatter={(v: number) => `$${v.toFixed(2)}`}
              tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
              axisLine={false}
              tickLine={false}
              width={60}
            />
            <Tooltip
              contentStyle={{
                background: 'hsl(var(--card))',
                border: '1px solid hsl(var(--border))',
                borderRadius: 8,
                fontSize: 11,
              }}
              formatter={(value) => [`$${Number(value).toFixed(4)}`, 'P&L']}
              labelFormatter={(label) => fmtDate(Number(label))}
            />
            <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeOpacity={0.3} strokeDasharray="2 2" />
            <Area
              type="monotone"
              dataKey="pnl"
              stroke={pnlColor}
              strokeWidth={1.5}
              fill="url(#pnlGrad)"
              dot={false}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
