import { useState } from 'react'
import { errorMessage, sessionRequest } from './runtime-api'

interface GatewayAccess {
  baseUrl: string
  apiKey: string
}

export function AccessPanel() {
  const [access, setAccess] = useState<GatewayAccess | null>(null)
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  async function reveal() {
    setLoading(true)
    setMessage('')
    setError('')
    try {
      setAccess(await sessionRequest<GatewayAccess>('/api/access'))
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setLoading(false)
    }
  }

  async function copy(value: string, label: string) {
    setError('')
    setMessage('')
    try {
      await navigator.clipboard.writeText(value)
      setMessage(`${label} copied.`)
    } catch {
      setError('Could not copy to the clipboard. Select and copy the value manually.')
    }
  }

  return (
    <section className="panel settings-panel" aria-labelledby="access-title">
      <div className="panel-header"><h2 id="access-title">Gateway access</h2></div>
      <div className="settings-body">
        <p className="hint">Use these settings in an OpenAI-compatible client. The gateway key is for this local runtime only and is not your provider API key.</p>
        {access && (
          <>
            <div className="settings-row">
              <label>Local API base URL<input readOnly value={access.baseUrl} /></label>
              <button type="button" onClick={() => void copy(access.baseUrl, 'Base URL')}>Copy URL</button>
            </div>
            <div className="settings-row">
              <label>Local gateway key<input readOnly value={access.apiKey} autoComplete="off" spellCheck={false} /></label>
              <button type="button" onClick={() => void copy(access.apiKey, 'Gateway key')}>Copy key</button>
            </div>
          </>
        )}
        <div className="actions">
          {access ? <button type="button" onClick={() => { setAccess(null); setError(''); setMessage('') }}>Hide credentials</button>
            : <button type="button" onClick={() => void reveal()} disabled={loading}>{loading ? 'Loading…' : 'Reveal connection details'}</button>}
        </div>
        {message && <p className="hint" role="status">{message}</p>}
        {error && <p className="error" role="alert">{error}</p>}
      </div>
    </section>
  )
}
