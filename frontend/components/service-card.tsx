'use client'

import { ServiceStatus } from '@/lib/api'

function fmtUptime(s: number): string {
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${m}m`
}

interface Props {
  service: ServiceStatus
  onStart?: () => void
  onStop?: () => void
  actionLoading?: boolean
}

export default function ServiceCard({ service, onStart, onStop, actionLoading }: Props) {
  const canControl = onStart !== undefined || onStop !== undefined

  return (
    <div className="bg-card border border-border rounded-xl p-5 flex flex-col gap-3">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs text-muted-foreground uppercase tracking-wider mb-1">{service.name}</p>
          <h3 className="text-sm font-medium text-foreground">{service.display}</h3>
        </div>
        <span
          className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
            service.active
              ? 'bg-emerald-500/15 text-emerald-400'
              : 'bg-muted text-muted-foreground'
          }`}
        >
          {service.active && (
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
          )}
          {service.active ? 'Running' : 'Stopped'}
        </span>
      </div>

      {/* Uptime */}
      {service.active && service.uptime_s !== undefined && (
        <p className="text-xs text-muted-foreground">
          Uptime: <span className="text-foreground/70 font-mono">{fmtUptime(service.uptime_s)}</span>
        </p>
      )}

      {/* Control buttons (recorder only) */}
      {canControl && (
        <div className="flex gap-2 mt-auto pt-1">
          {service.active ? (
            <button
              onClick={onStop}
              disabled={actionLoading}
              className="flex-1 py-1.5 rounded-lg text-xs font-medium bg-red-500/10 text-red-400 hover:bg-red-500/20 border border-red-500/20 transition-colors disabled:opacity-50"
            >
              {actionLoading ? 'Stopping…' : 'Stop'}
            </button>
          ) : (
            <button
              onClick={onStart}
              disabled={actionLoading}
              className="flex-1 py-1.5 rounded-lg text-xs font-medium bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 border border-emerald-500/20 transition-colors disabled:opacity-50"
            >
              {actionLoading ? 'Starting…' : 'Start'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
