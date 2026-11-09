import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import mark from '../public/favicon.svg?url'
import './runtime-console.css'

interface Health {
  status: 'ok'
  service: 'opencodex-lite'
  runtime: 'python'
  version: string
  pid: number
  port: number
  startedAt: string
  uptimeSeconds: number
}

interface Details extends Health {
  logPath: string
  stateDirectory: string
}

type Snapshot =
  | { state: 'checking' | 'disconnected' }
  | { state: 'running'; health: Health; details?: Details }

type StopRequest = { phase: 'sending' | 'accepted'; instance: string }

function instanceKey({ pid, startedAt }: Health) {
  return `${pid}:${startedAt}`
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const timeout = AbortSignal.timeout(3000)
  const response = await fetch(path, {
    ...options,
    signal: options?.signal ? AbortSignal.any([options.signal, timeout]) : timeout,
    cache: 'no-store',
    redirect: 'error',
  })
  if (!response.ok) throw new Error(`Request failed (HTTP ${response.status}).`)
  return response.json() as Promise<T>
}

async function sessionToken(signal?: AbortSignal) {
  const { token } = await request<{ token: string }>('/opencodex-session', { signal })
  if (typeof token !== 'string' || !token) throw new Error('No session token was returned.')
  return token
}

function uptime(seconds: number) {
  const total = Math.floor(seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  return [hours && `${hours}h`, (hours || minutes) && `${minutes}m`, `${total % 60}s`]
    .filter(Boolean)
    .join(' ')
}

function RuntimeConsole() {
  const [snapshot, setSnapshot] = useState<Snapshot>({ state: 'checking' })
  const [stopRequest, setStopRequest] = useState<StopRequest | null>(null)
  const [stopError, setStopError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    let timer: number
    let session: { instance: string; token: string } | undefined

    async function refresh() {
      try {
        const health = await request<Health>('/healthz', { signal })
        if (health.status !== 'ok' || health.service !== 'opencodex-lite' || health.runtime !== 'python') {
          throw new Error('Not an OpenCodex Lite Python runtime.')
        }

        const instance = instanceKey(health)
        let details: Details | undefined
        try {
          if (session?.instance !== instance) session = { instance, token: await sessionToken(signal) }
          const status = await request<Details>('/api/status', {
            signal,
            headers: { Authorization: `Bearer ${session.token}` },
          })
          if (instanceKey(status) === instance) details = status
        } catch {
          session = undefined
        }
        if (!signal.aborted) setSnapshot({ state: 'running', health, details })
      } catch {
        session = undefined
        if (!signal.aborted) setSnapshot({ state: 'disconnected' })
      } finally {
        if (!signal.aborted) timer = window.setTimeout(refresh, 3000)
      }
    }

    void refresh()
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [])

  async function stop() {
    if (snapshot.state !== 'running' || !window.confirm('Stop the local OpenCodex Lite runtime?')) return
    const instance = instanceKey(snapshot.health)
    setStopError('')
    setStopRequest({ phase: 'sending', instance })
    try {
      const token = await sessionToken()
      const { status } = await request<{ status: string }>('/api/stop', {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (status !== 'stopping') throw new Error('Shutdown was not confirmed.')
      setStopRequest({ phase: 'accepted', instance })
    } catch {
      setStopRequest(null)
      setStopError('Could not confirm shutdown. Check the status below or run uv run ocx status.')
    }
  }

  const running = snapshot.state === 'running'
  const stopping = running && stopRequest?.instance === instanceKey(snapshot.health)
  const disconnected = snapshot.state === 'disconnected'
  const label = stopping ? 'Stopping…' : running ? 'Running' : disconnected ? 'Disconnected' : 'Checking…'

  return (
    <main>
      <link rel="icon" type="image/svg+xml" href={mark} />
      <header className="brand">
        <img src={mark} alt="" width="32" height="32" />
        <span>OpenCodex Lite</span>
      </header>

      <h1>Runtime console</h1>
      <p className="intro">Monitor and stop the local Python runtime.</p>

      <section className="panel" aria-labelledby="status-title">
        <div className="panel-header">
          <h2 id="status-title">Runtime status</h2>
          <span className="status" data-running={running && !stopping} role="status">{label}</span>
        </div>

        {snapshot.state === 'running' && (
          <dl>
            <div><dt>Version</dt><dd>{snapshot.health.version}</dd></div>
            <div><dt>Port</dt><dd>{snapshot.health.port}</dd></div>
            <div><dt>Process ID</dt><dd>{snapshot.health.pid}</dd></div>
            <div><dt>Uptime</dt><dd>{uptime(snapshot.health.uptimeSeconds)}</dd></div>
            {snapshot.details && <>
              <div className="path"><dt>Log file</dt><dd>{snapshot.details.logPath}</dd></div>
              <div className="path"><dt>State directory</dt><dd>{snapshot.details.stateDirectory}</dd></div>
            </>}
          </dl>
        )}

        {snapshot.state === 'running' && !snapshot.details && (
          <p className="notice">Runtime is responding, but local paths are unavailable.</p>
        )}
        {disconnected && (
          <p className="notice">
            {stopRequest?.phase === 'accepted' ? 'The runtime is no longer responding after shutdown was requested.' : 'The console cannot reach the Python runtime.'}
            {' '}To start it, run <code>uv run ocx start</code> from the project directory.
          </p>
        )}
        {stopping && stopRequest?.phase === 'accepted' && (
          <p className="notice" role="status">Shutdown requested. To start it again, run <code>uv run ocx start</code> from the project directory.</p>
        )}

        <div className="panel-footer">
          <button type="button" onClick={() => void stop()} disabled={!running || stopping}>Stop runtime</button>
          {stopError && <p className="error" role="alert">{stopError}</p>}
        </div>
      </section>

      <p className="scope">Provider APIs and OAuth are not available in this migration stage.</p>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(<StrictMode><RuntimeConsole /></StrictMode>)
