'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  Cell, PieChart, Pie, Legend,
} from 'recharts'
import {
  getTradeBreakdown, getHourlyPerformance,
  type BreakdownEntry, type HourlyBucket,
} from '@/lib/api'

const REASON_COLORS: Record<string, string> = {
  take_profit: '#34d399',
  stop_loss: '#f87171',
  timeout: '#fbbf24',
}

const SIDE_COLORS: Record<string, string> = {
  BUY: '#34d399',
  SELL: '#f87171',
}

interface Props {
  symbol?: string
  days?: number
}

function BreakdownTable({ title, data }: { title: string; data: BreakdownEntry[] }) {
  if (data.length === 0) return null

  return (
    <div>
      <h3 className="text-xs text-muted-foreground uppercase tracking-wider mb-2">{title}</h3>
      <table className="w-full text-xs font-mono">
        <thead>
          <tr className="border-b border-border">
            <th className="text-left pb-1.5 text-muted-foreground font-normal"></th>
            <th className="text-right pb-1.5 text-muted-foreground font-normal">Trades</th>
            <th className="text-right pb-1.5 text-muted-foreground font-normal">Win%</th>
            <th className="text-right pb-1.5 text-muted-foreground font-normal">Avg bps</th>
            <th className="text-right pb-1.5 text-muted-foreground font-normal">Net USD</th>
          </tr>
        </thead>
        <tbody>
          {data.map(d => (
            <tr key={d.label} className="border-b border-border/30">
              <td className="py-1.5 text-foreground/80 capitalize">{d.label.replace('_', ' ')}</td>
              <td className="py-1.5 text-right text-muted-foreground">{d.count}</td>
              <td className="py-1.5 text-right text-muted-foreground">{d.win_rate.toFixed(1)}%</td>
              <td className={`py-1.5 text-right ${d.avg_bps >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {d.avg_bps >= 0 ? '+' : ''}{d.avg_bps.toFixed(2)}
              </td>
              <td className={`py-1.5 text-right font-medium ${d.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {d.net_pnl >= 0 ? '+' : ''}${d.net_pnl.toFixed(4)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function TradeBreakdownCard({ symbol, days = 7 }: Props) {
  const [breakdown, setBreakdown] = useState<{
    by_exit_reason: BreakdownEntry[]
    by_side: BreakdownEntry[]
    by_symbol: BreakdownEntry[]
  } | null>(null)

  const refresh = useCallback(async () => {
    try {
      setBreakdown(await getTradeBreakdown(symbol, days))
    } catch { /* keep previous */ }
  }, [symbol, days])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 30_000)
    return () => clearInterval(t)
  }, [refresh])

  if (!breakdown) {
    return (
      <div className="bg-card border border-border rounded-xl p-5">
        <h2 className="text-sm font-medium text-foreground mb-4">Trade Breakdown</h2>
        <p className="text-xs text-muted-foreground font-mono">Loading…</p>
      </div>
    )
  }

  const hasData = breakdown.by_exit_reason.length > 0
  const pieData = breakdown.by_exit_reason.map(d => ({
    name: d.label.replace('_', ' '),
    value: d.count,
    fill: REASON_COLORS[d.label] ?? '#888',
  }))

  return (
    <div className="bg-card border border-border rounded-xl p-5 space-y-5">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium text-foreground">Trade Breakdown</h2>
        <span className="text-xs text-muted-foreground">{days}d</span>
      </div>

      {!hasData ? (
        <p className="text-xs text-muted-foreground font-mono">No trades to analyze</p>
      ) : (
        <>
          {/* Exit reason pie chart */}
          <div className="flex justify-center">
            <div className="w-48 h-44">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={pieData}
                    innerRadius={32}
                    outerRadius={56}
                    paddingAngle={2}
                    dataKey="value"
                    isAnimationActive={false}
                  >
                    {pieData.map((entry, i) => (
                      <Cell key={i} fill={entry.fill} />
                    ))}
                  </Pie>
                  <Legend
                    iconSize={8}
                    wrapperStyle={{ fontSize: 10, color: 'rgba(255,255,255,0.7)' }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>
          <BreakdownTable title="By Exit Reason" data={breakdown.by_exit_reason} />

          <BreakdownTable title="By Side" data={breakdown.by_side} />
          {!symbol && <BreakdownTable title="By Symbol" data={breakdown.by_symbol} />}
        </>
      )}
    </div>
  )
}

export function HourlyPerformanceCard({ symbol, days = 7 }: Props) {
  const [hourly, setHourly] = useState<HourlyBucket[]>([])

  const refresh = useCallback(async () => {
    try {
      setHourly(await getHourlyPerformance(symbol, days))
    } catch { /* keep previous */ }
  }, [symbol, days])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 30_000)
    return () => clearInterval(t)
  }, [refresh])

  const hasData = hourly.some(h => h.count > 0)

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-medium text-foreground">Hourly Performance (UTC)</h2>
        <span className="text-xs text-muted-foreground">{days}d</span>
      </div>

      {!hasData ? (
        <div className="h-64 flex items-center justify-center text-xs text-muted-foreground font-mono">
          No trade data yet
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={hourly} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" strokeOpacity={0.4} />
            <XAxis
              dataKey="hour"
              tickFormatter={(h: number) => `${h}h`}
              tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.7)' }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              tickFormatter={(v: number) => `$${v.toFixed(2)}`}
              tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.7)' }}
              axisLine={false}
              tickLine={false}
              width={50}
            />
            <Tooltip
              contentStyle={{
                background: 'hsl(var(--card))',
                border: '1px solid hsl(var(--border))',
                borderRadius: 8,
                fontSize: 11,
              }}
              formatter={(value, name) => {
                if (name === 'net_pnl') return [`$${Number(value).toFixed(4)}`, 'Net P&L']
                return [value, name]
              }}
              labelFormatter={(h) => `${h}:00 UTC`}
            />
            <Bar dataKey="net_pnl" isAnimationActive={false} radius={[2, 2, 0, 0]}>
              {hourly.map((entry, i) => (
                <Cell
                  key={i}
                  fill={entry.net_pnl >= 0 ? '#34d399' : '#f87171'}
                  fillOpacity={entry.count > 0 ? 0.8 : 0.15}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
