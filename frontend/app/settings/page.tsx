'use client'

import { useEffect, useState } from 'react'
import { getConfig, updateConfig, type TradingConfig } from '@/lib/api'

type StrategyDraft = {
  window_ms: number
  trade_count_trigger: number
  move_bps_trigger: number
  intensity_spike_mult: number
  afi_threshold: number
  obi_threshold: number
  cooldown_ms: number
  take_profit_bps: number
  stop_loss_bps: number
  max_hold_ms: number
  macro_trend_halflife_ms: number
  macro_trend_warmup_ms: number
}

type SymbolOverrideDraft = {
  obi_threshold: number
  intensity_spike_mult: number
  move_bps_trigger: number
  take_profit_bps: number
  stop_loss_bps: number
  max_hold_ms: number
}

type RiskDraft = {
  daily_loss_usd: number
  max_spread_bps: number
  max_ws_lag_ms: number
  max_trades_per_min: number
  max_consecutive_losses: number
}

function Field({
  label,
  value,
  onChange,
  step = 1,
  min,
  hint,
}: {
  label: string
  value: number
  onChange: (v: number) => void
  step?: number
  min?: number
  hint?: string
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <input
        type="number"
        value={value}
        step={step}
        min={min}
        onChange={e => onChange(parseFloat(e.target.value) || 0)}
        className="bg-input border border-border rounded-lg px-3 py-1.5 text-sm text-foreground font-mono focus:outline-none focus:ring-1 focus:ring-ring w-full"
      />
      {hint && <span className="text-xs text-muted-foreground/50">{hint}</span>}
    </label>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <h2 className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-4">{title}</h2>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
        {children}
      </div>
    </div>
  )
}

export default function SettingsPage() {
  const [config, setConfig]     = useState<TradingConfig | null>(null)
  const [strategy, setStrategy] = useState<StrategyDraft | null>(null)
  const [btc, setBtc]           = useState<SymbolOverrideDraft | null>(null)
  const [eth, setEth]           = useState<SymbolOverrideDraft | null>(null)
  const [risk, setRisk]         = useState<RiskDraft | null>(null)
  const [saving, setSaving]     = useState(false)
  const [saved, setSaved]       = useState(false)
  const [error, setError]       = useState<string | null>(null)

  useEffect(() => {
    getConfig()
      .then(c => {
        setConfig(c)
        setStrategy({
          window_ms: c.strategy.window_ms,
          trade_count_trigger: c.strategy.trade_count_trigger,
          move_bps_trigger: c.strategy.move_bps_trigger,
          intensity_spike_mult: c.strategy.intensity_spike_mult,
          afi_threshold: c.strategy.afi_threshold,
          obi_threshold: c.strategy.obi_threshold,
          cooldown_ms: c.strategy.cooldown_ms,
          take_profit_bps: c.strategy.exit.take_profit_bps,
          stop_loss_bps: c.strategy.exit.stop_loss_bps,
          max_hold_ms: c.strategy.exit.max_hold_ms,
          macro_trend_halflife_ms: c.strategy.macro_trend_halflife_ms,
          macro_trend_warmup_ms: c.strategy.macro_trend_warmup_ms,
        })
        
        const b = c.strategy.symbol_overrides?.['BTCUSDT']
        setBtc({
          obi_threshold: b?.obi_threshold ?? c.strategy.obi_threshold,
          intensity_spike_mult: b?.intensity_spike_mult ?? c.strategy.intensity_spike_mult,
          move_bps_trigger: b?.move_bps_trigger ?? c.strategy.move_bps_trigger,
          take_profit_bps: b?.exit?.take_profit_bps ?? c.strategy.exit.take_profit_bps,
          stop_loss_bps: b?.exit?.stop_loss_bps ?? c.strategy.exit.stop_loss_bps,
          max_hold_ms: b?.exit?.max_hold_ms ?? c.strategy.exit.max_hold_ms,
        })

        const e = c.strategy.symbol_overrides?.['ETHUSDT']
        setEth({
          obi_threshold: e?.obi_threshold ?? c.strategy.obi_threshold,
          intensity_spike_mult: e?.intensity_spike_mult ?? c.strategy.intensity_spike_mult,
          move_bps_trigger: e?.move_bps_trigger ?? c.strategy.move_bps_trigger,
          take_profit_bps: e?.exit?.take_profit_bps ?? c.strategy.exit.take_profit_bps,
          stop_loss_bps: e?.exit?.stop_loss_bps ?? c.strategy.exit.stop_loss_bps,
          max_hold_ms: e?.exit?.max_hold_ms ?? c.strategy.exit.max_hold_ms,
        })

        setRisk({
          daily_loss_usd: c.risk.daily_loss_usd,
          max_spread_bps: c.risk.max_spread_bps,
          max_ws_lag_ms: c.risk.max_ws_lag_ms,
          max_trades_per_min: c.risk.max_trades_per_min,
          max_consecutive_losses: c.risk.max_consecutive_losses,
        })
      })
      .catch(() => setError('Failed to load config'))
  }, [])

  const setS = (k: keyof StrategyDraft) => (v: number) =>
    setStrategy(p => p ? { ...p, [k]: v } : p)

  const setB = (k: keyof SymbolOverrideDraft) => (v: number) =>
    setBtc(p => p ? { ...p, [k]: v } : p)
    
  const setE = (k: keyof SymbolOverrideDraft) => (v: number) =>
    setEth(p => p ? { ...p, [k]: v } : p)

  const setR = (k: keyof RiskDraft) => (v: number) =>
    setRisk(p => p ? { ...p, [k]: v } : p)

  async function save() {
    if (!strategy || !risk || !config || !btc || !eth) return
    setSaving(true)
    setError(null)
    try {
      await updateConfig({
        strategy: {
          ...config.strategy,
          window_ms: strategy.window_ms,
          trade_count_trigger: strategy.trade_count_trigger,
          move_bps_trigger: strategy.move_bps_trigger,
          intensity_spike_mult: strategy.intensity_spike_mult,
          afi_threshold: strategy.afi_threshold,
          obi_threshold: strategy.obi_threshold,
          cooldown_ms: strategy.cooldown_ms,
          macro_trend_halflife_ms: strategy.macro_trend_halflife_ms,
          macro_trend_warmup_ms: strategy.macro_trend_warmup_ms,
          exit: {
            ...config.strategy.exit,
            take_profit_bps: strategy.take_profit_bps,
            stop_loss_bps: strategy.stop_loss_bps,
            max_hold_ms: strategy.max_hold_ms,
          },
          symbol_overrides: {
            BTCUSDT: {
              obi_threshold: btc.obi_threshold,
              intensity_spike_mult: btc.intensity_spike_mult,
              move_bps_trigger: btc.move_bps_trigger,
              exit: {
                take_profit_bps: btc.take_profit_bps,
                stop_loss_bps: btc.stop_loss_bps,
                max_hold_ms: btc.max_hold_ms,
              }
            },
            ETHUSDT: {
              obi_threshold: eth.obi_threshold,
              intensity_spike_mult: eth.intensity_spike_mult,
              move_bps_trigger: eth.move_bps_trigger,
              exit: {
                take_profit_bps: eth.take_profit_bps,
                stop_loss_bps: eth.stop_loss_bps,
                max_hold_ms: eth.max_hold_ms,
              }
            }
          }
        },
        risk: {
          ...config.risk,
          daily_loss_usd: risk.daily_loss_usd,
          max_spread_bps: risk.max_spread_bps,
          max_ws_lag_ms: risk.max_ws_lag_ms,
          max_trades_per_min: risk.max_trades_per_min,
          max_consecutive_losses: risk.max_consecutive_losses,
        },
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  if (!strategy || !risk) {
    return (
      <div className="p-6">
        <p className="text-sm text-muted-foreground">{error ?? 'Loading config…'}</p>
      </div>
    )
  }

  return (
    <div className="p-6 w-full max-w-5xl space-y-5">
      <div>
        <h1 className="text-lg font-semibold text-foreground">Settings</h1>
        <p className="text-sm text-muted-foreground mt-0.5">Strategy and risk parameters</p>
      </div>

      {/* Global Entry / Detection */}
      <Section title="Global Entry — Detection">
        <Field label="Window (ms)" value={strategy.window_ms} onChange={setS('window_ms')} step={10} min={50}
          hint="Burst detection window" />
        <Field label="Trade count trigger" value={strategy.trade_count_trigger} onChange={setS('trade_count_trigger')} min={1}
          hint="Aggressor trades in window" />
        <Field label="Move trigger (bps)" value={strategy.move_bps_trigger} onChange={setS('move_bps_trigger')} step={0.1} min={0}
          hint="Min mid-price move" />
        <Field label="Intensity Mult" value={strategy.intensity_spike_mult} onChange={setS('intensity_spike_mult')} step={0.5} min={0}
          hint="Global intensity floor" />
        <Field label="AFI Threshold" value={strategy.afi_threshold} onChange={setS('afi_threshold')} step={0.05} min={0}
          hint="Aggressive flow ratio" />
        <Field label="OBI Threshold" value={strategy.obi_threshold} onChange={setS('obi_threshold')} step={0.01} min={0}
          hint="Order book imbalance" />
        <Field label="Cooldown (ms)" value={strategy.cooldown_ms} onChange={setS('cooldown_ms')} step={100} min={0}
          hint="Wait after last trade" />
      </Section>

      {/* Global Macro Filter */}
      <Section title="Global Macro Trend">
        <Field label="EMA Halflife (ms)" value={strategy.macro_trend_halflife_ms} onChange={setS('macro_trend_halflife_ms')} step={60000} min={1000}
          hint="15m = 900,000ms" />
        <Field label="Warmup (ms)" value={strategy.macro_trend_warmup_ms} onChange={setS('macro_trend_warmup_ms')} step={60000} min={0} />
      </Section>

      {/* Global Exit Defaults */}
      <Section title="Global Exit Defaults">
        <Field label="Take profit (bps)" value={strategy.take_profit_bps} onChange={setS('take_profit_bps')} step={0.5} min={0} />
        <Field label="Stop loss (bps)" value={strategy.stop_loss_bps} onChange={setS('stop_loss_bps')} step={0.5} min={0} />
        <Field label="Max hold (ms)" value={strategy.max_hold_ms} onChange={setS('max_hold_ms')} step={100} min={100} />
      </Section>

      {/* BTCUSDT Overrides */}
      {btc && (
        <Section title="BTCUSDT Overrides">
          <Field label="OBI Threshold" value={btc.obi_threshold} onChange={setB('obi_threshold')} step={0.01} min={0} />
          <Field label="Intensity Mult" value={btc.intensity_spike_mult} onChange={setB('intensity_spike_mult')} step={0.5} min={0} />
          <Field label="Move Trigger (bps)" value={btc.move_bps_trigger} onChange={setB('move_bps_trigger')} step={0.1} min={0} />
          <Field label="Take Profit (bps)" value={btc.take_profit_bps} onChange={setB('take_profit_bps')} step={0.5} min={0} />
          <Field label="Stop Loss (bps)" value={btc.stop_loss_bps} onChange={setB('stop_loss_bps')} step={0.5} min={0} />
          <Field label="Max Hold (ms)" value={btc.max_hold_ms} onChange={setB('max_hold_ms')} step={100} min={100} />
        </Section>
      )}

      {/* ETHUSDT Overrides */}
      {eth && (
        <Section title="ETHUSDT Overrides">
          <Field label="OBI Threshold" value={eth.obi_threshold} onChange={setE('obi_threshold')} step={0.01} min={0} />
          <Field label="Intensity Mult" value={eth.intensity_spike_mult} onChange={setE('intensity_spike_mult')} step={0.5} min={0} />
          <Field label="Move Trigger (bps)" value={eth.move_bps_trigger} onChange={setE('move_bps_trigger')} step={0.1} min={0} />
          <Field label="Take Profit (bps)" value={eth.take_profit_bps} onChange={setE('take_profit_bps')} step={0.5} min={0} />
          <Field label="Stop Loss (bps)" value={eth.stop_loss_bps} onChange={setE('stop_loss_bps')} step={0.5} min={0} />
          <Field label="Max Hold (ms)" value={eth.max_hold_ms} onChange={setE('max_hold_ms')} step={100} min={100} />
        </Section>
      )}

      {/* Risk */}
      <Section title="Risk Limits">
        <Field label="Daily loss limit ($)" value={risk.daily_loss_usd} onChange={setR('daily_loss_usd')} step={5} min={0} />
        <Field label="Max spread (bps)" value={risk.max_spread_bps} onChange={setR('max_spread_bps')} step={0.5} min={0} />
        <Field label="Max WS lag (ms)" value={risk.max_ws_lag_ms} onChange={setR('max_ws_lag_ms')} step={50} min={50} />
        <Field label="Max trades / min" value={risk.max_trades_per_min} onChange={setR('max_trades_per_min')} min={1} />
        <Field label="Max consec. losses" value={risk.max_consecutive_losses} onChange={setR('max_consecutive_losses')} min={1} />
      </Section>

      {/* Save */}
      <div className="flex items-center gap-3 pt-1">
        <button
          onClick={save}
          disabled={saving}
          className="px-5 py-2 rounded-lg text-sm font-medium bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 transition-opacity"
        >
          {saving ? 'Saving…' : 'Save settings'}
        </button>
        {saved  && <span className="text-sm text-emerald-400">Saved ✓</span>}
        {error  && <span className="text-sm text-red-400">{error}</span>}
        <span className="text-xs text-muted-foreground/40 ml-auto">Restart services to apply strategy changes</span>
      </div>
    </div>
  )
}
