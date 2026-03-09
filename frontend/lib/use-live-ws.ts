'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import type { LiveState } from './api'

const WS_BASE = (process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000')
  .replace(/^http/, 'ws') + '/api/ws/live'

export function useLiveWs(): {
  state: LiveState | null
  connected: boolean
} {
  const [state, setState] = useState<LiveState | null>(null)
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const retryRef = useRef(0)
  const mountedRef = useRef(true)

  const connect = useCallback(() => {
    if (!mountedRef.current) return

    try {
      const ws = new WebSocket(WS_BASE)
      wsRef.current = ws

      ws.onopen = () => {
        setConnected(true)
        retryRef.current = 0
      }

      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data) as LiveState
          setState(data)
        } catch { /* ignore malformed */ }
      }

      ws.onclose = () => {
        setConnected(false)
        wsRef.current = null
        if (mountedRef.current) {
          const delay = Math.min(1000 * 2 ** retryRef.current, 10_000)
          retryRef.current++
          setTimeout(connect, delay)
        }
      }

      ws.onerror = () => {
        ws.close()
      }
    } catch {
      const delay = Math.min(1000 * 2 ** retryRef.current, 10_000)
      retryRef.current++
      setTimeout(connect, delay)
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      wsRef.current?.close()
    }
  }, [connect])

  return { state, connected }
}
