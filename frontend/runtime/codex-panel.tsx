import { useEffect, useState } from 'react'
import { errorMessage, sessionRequest } from './runtime-api'

interface CodexStatus {
  configPath: string
  configExists?: boolean
  state: 'notConfigured' | 'configured' | 'conflict'
  reason?: string
}

interface CodexPreview extends CodexStatus {
  baseUrl: string
  model: string
}

type PreviewRequest = { home: string }
type PreviewResult = {
  request: PreviewRequest
  configRevision: number
  preview?: CodexPreview
  error?: string
}

const statusLabels = {
  notConfigured: 'Not configured',
  configured: 'Configured',
  conflict: 'Conflict',
}

export function CodexPanel({ configRevision }: { configRevision: number }) {
  const [home, setHome] = useState('')
  const [request, setRequest] = useState<PreviewRequest>({ home: '' })
  const [result, setResult] = useState<PreviewResult>()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    const query = request.home ? `?${new URLSearchParams({ home: request.home })}` : ''
    void sessionRequest<CodexPreview>(`/api/clients/codex${query}`, { signal: controller.signal })
      .then(preview => {
        if (!controller.signal.aborted) setResult({ request, configRevision, preview })
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted) setResult({ request, configRevision, error: errorMessage(failure) })
      })
    return () => controller.abort()
  }, [request, configRevision])

  const matchesHome = request.home === home.trim()
  const current = matchesHome && result?.request === request && result.configRevision === configRevision ? result : undefined
  const preview = current?.preview

  async function change(action: 'configure' | 'restore') {
    if (!preview || busy || preview.state === 'conflict') return
    const restoring = action === 'restore'
    if (restoring ? preview.state !== 'configured' : !preview.model) return
    const prompt = restoring
      ? `Restore the Codex routing settings in ${preview.configPath}?`
      : `Configure ${preview.configPath} to use ${preview.model} through ${preview.baseUrl}?`
    if (!window.confirm(prompt)) return
    setBusy(true)
    setError('')
    setMessage('')
    try {
      const status = await sessionRequest<CodexStatus>(`/api/clients/codex/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request.home ? { home: request.home } : {}),
      })
      if (status.state !== (restoring ? 'notConfigured' : 'configured')) {
        throw new Error(status.reason || 'The change was not confirmed.')
      }
      setMessage(restoring ? 'Previous Codex routing restored. Restart Codex to use it.' : 'Codex routing saved. Restart Codex to use the gateway.')
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setRequest({ home: request.home })
      setBusy(false)
    }
  }

  return (
    <section className="panel settings-panel" aria-labelledby="codex-title">
      <div className="panel-header">
        <h2 id="codex-title">Codex client</h2>
        {preview && <span className="status" data-running={preview.state === 'configured'} data-pending={preview.state === 'conflict'}>
          {statusLabels[preview.state]}
        </span>}
      </div>
      <div className="settings-body">
        <p className="hint">Point Codex at the saved gateway defaults. Codex needs a provider with native Responses support. This changes routing in <code>config.toml</code>, not your Codex login or history.</p>
        <form className="settings-row" onSubmit={event => {
          event.preventDefault()
          setError('')
          setMessage('')
          setRequest({ home: home.trim() })
        }}>
          <label>Codex home directory
            <input value={home} disabled={busy} onChange={event => {
              setHome(event.target.value)
              setError('')
              setMessage('')
            }} placeholder="Leave blank to use the default" autoComplete="off" spellCheck={false} />
          </label>
          <button type="submit" disabled={busy}>Preview settings</button>
        </form>
        {preview ? (
          <>
            <dl className="preview-details">
              <div className="path"><dt>Config file{preview.configExists === false ? ' (not created yet)' : ''}</dt><dd>{preview.configPath}</dd></div>
              <div><dt>Gateway base URL</dt><dd>{preview.baseUrl}</dd></div>
              <div><dt>Saved gateway model</dt><dd>{preview.model || 'None'}</dd></div>
            </dl>
            {preview.state === 'conflict' && <p className="error" role="alert">{preview.reason || 'Codex routing cannot be changed safely.'}</p>}
            {!preview.model && <p className="hint">Save a default provider and model before configuring Codex.</p>}
          </>
        ) : current?.error ? (
          <p className="error" role="alert">{current.error}</p>
        ) : (
          <p className="hint" role="status">{matchesHome ? 'Checking Codex configuration…' : 'Preview this directory before making changes.'}</p>
        )}
      </div>
      <div className="panel-footer">
        <button className="primary-button" type="button" disabled={busy || !preview?.model || preview.state === 'conflict'}
          onClick={() => void change('configure')}>{preview?.state === 'configured' ? 'Update Codex' : 'Configure Codex'}</button>
        <button type="button" disabled={busy || preview?.state !== 'configured'} onClick={() => void change('restore')}>Restore previous routing</button>
        {error && <p className="error feedback" role="alert">{error}</p>}
        {(busy || message) && <p className="hint feedback" role="status">{busy ? 'Changing Codex routing…' : message}</p>}
      </div>
    </section>
  )
}
