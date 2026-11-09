import { useEffect, useRef, useState } from 'react'
import { errorMessage, sessionRequest } from './runtime-api'
import './provider-panel.css'

interface Provider {
  id: string
  name: string
  baseUrl: string
  apiKeyEnv: string | null
  hasKey: boolean
  responses: boolean
  models: string[]
}

interface Config {
  providers: Provider[]
  defaultProvider: string | null
  defaultModel: string | null
}

interface ProviderDraft extends Provider {
  draftKey: string
  apiKey: string
}

interface ConfigDraft extends Omit<Config, 'providers'> {
  providers: ProviderDraft[]
}

interface Discovery {
  provider: string
  models: string[]
}

function draftFrom(config: Config): ConfigDraft {
  return {
    ...config,
    providers: config.providers.map(provider => ({ ...provider, draftKey: provider.id, apiKey: '' })),
  }
}

function savePayload(draft: ConfigDraft) {
  const defaultProvider = draft.providers.find(provider => provider.draftKey === draft.defaultProvider)
  return {
    providers: draft.providers.map(provider => ({
      id: provider.id.trim(),
      name: provider.name.trim(),
      baseUrl: provider.baseUrl.trim(),
      apiKeyEnv: provider.apiKey.trim() ? null : provider.apiKeyEnv?.trim() || null,
      ...(provider.apiKey.trim() ? { apiKey: provider.apiKey.trim() } : {}),
      responses: provider.responses,
      models: provider.models,
    })),
    defaultProvider: defaultProvider?.id.trim() || null,
    defaultModel: defaultProvider ? draft.defaultModel : null,
  }
}

const jsonHeaders = { 'Content-Type': 'application/json' }

function usesSavedConnection(draft: ProviderDraft | undefined, saved: Provider | undefined) {
  return Boolean(draft && saved &&
    draft.baseUrl.trim() === saved.baseUrl &&
    (draft.apiKeyEnv?.trim() || '') === (saved.apiKeyEnv ?? '') &&
    !draft.apiKey.trim() && draft.responses === saved.responses)
}

export function ProviderPanel({ onSaved }: { onSaved: () => void }) {
  const [saved, setSaved] = useState<Config | null>(null)
  const [draft, setDraft] = useState<ConfigDraft | null>(null)
  const [selectedKey, setSelectedKey] = useState('')
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [discovery, setDiscovery] = useState<Discovery | null>(null)
  const lifetime = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    lifetime.current = controller
    void sessionRequest<Config>('/api/config', { signal: controller.signal }).then(config => {
      if (controller.signal.aborted) return
      setSaved(config)
      setDraft(draftFrom(config))
      setSelectedKey(config.defaultProvider ?? config.providers[0]?.id ?? '')
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(errorMessage(failure))
    })
    return () => controller.abort()
  }, [loadAttempt])

  async function run(label: string, action: (signal: AbortSignal) => Promise<void>) {
    const signal = lifetime.current?.signal
    if (busy || !signal || signal.aborted) return
    setBusy(label)
    setError('')
    setMessage('')
    try {
      await action(signal)
    } catch (failure) {
      if (!signal.aborted) setError(errorMessage(failure))
    } finally {
      if (!signal.aborted) setBusy('')
    }
  }

  if (!saved || !draft) {
    return (
      <section className="panel settings-panel" aria-labelledby="providers-title">
        <div className="panel-header"><h2 id="providers-title">API providers</h2></div>
        <div className="settings-body">
          {error ? <>
            <p className="error" role="alert">{error}</p>
            <button type="button" onClick={() => { setError(''); setLoadAttempt(loadAttempt + 1) }}>Retry</button>
          </> : <p role="status">Loading provider settings…</p>}
        </div>
      </section>
    )
  }

  const selected = draft.providers.find(provider => provider.draftKey === selectedKey)
  const savedProvider = saved.providers.find(provider => provider.id === selectedKey)
  const defaultProvider = draft.providers.find(provider => provider.draftKey === draft.defaultProvider)
  const hasPastedKey = Boolean(selected?.apiKey.trim())
  const payload = savePayload(draft)
  const dirty = JSON.stringify(payload) !== JSON.stringify(savePayload(draftFrom(saved)))
  const connectionSaved = usesSavedConnection(selected, savedProvider)
  const options = connectionSaved && discovery?.provider === selectedKey ? discovery.models : undefined

  function updateProvider(patch: Partial<ProviderDraft>) {
    setDraft(current => current && {
      ...current,
      providers: current.providers.map(provider => provider.draftKey === selectedKey ? { ...provider, ...patch } : provider),
      defaultModel: current.defaultProvider === selectedKey && patch.models && !patch.models.includes(current.defaultModel ?? '')
        ? null : current.defaultModel,
    })
  }

  function addProvider(deepSeek: boolean) {
    const provider: ProviderDraft = {
      draftKey: crypto.randomUUID(),
      id: deepSeek ? 'deepseek' : '',
      name: deepSeek ? 'DeepSeek' : '',
      baseUrl: deepSeek ? 'https://api.deepseek.com' : '',
      apiKeyEnv: deepSeek ? 'deepseek_api_key' : null,
      hasKey: false,
      apiKey: '',
      responses: deepSeek,
      models: [],
    }
    setDraft(current => current && { ...current, providers: [...current.providers, provider] })
    chooseProvider(provider.draftKey)
  }

  function chooseProvider(key: string) {
    setSelectedKey(key)
    setError('')
    setMessage('')
  }

  function removeProvider() {
    if (!selected || !draft || !window.confirm(`Remove ${selected.name.trim() || 'this provider'} from the draft? Save to apply the removal.`)) return
    const providers = draft.providers.filter(provider => provider.draftKey !== selectedKey)
    setDraft({
      providers,
      defaultProvider: draft.defaultProvider === selectedKey ? null : draft.defaultProvider,
      defaultModel: draft.defaultProvider === selectedKey ? null : draft.defaultModel,
    })
    chooseProvider(providers[0]?.draftKey ?? '')
  }

  const save = () => {
    const invalid = payload.providers.find(provider => !/^[a-z][a-z0-9_-]{0,47}$/.test(provider.id) || !provider.name || !provider.baseUrl)
    if (invalid) {
      const index = payload.providers.indexOf(invalid)
      setSelectedKey(draft.providers[index].draftKey)
      setError('Enter a name and base URL. The ID must start with a lowercase letter and may contain up to 48 lowercase letters, numbers, hyphens, or underscores.')
      return
    }
    if (new Set(payload.providers.map(provider => provider.id)).size !== payload.providers.length) {
      setError('Provider IDs must be unique.')
      return
    }
    const reused = draft.providers.find(provider => provider.draftKey !== provider.id.trim() &&
      saved.providers.some(prior => prior.id === provider.id.trim()))
    if (reused) {
      setSelectedKey(reused.draftKey)
      setError('Save the removal of the previous provider before reusing its ID.')
      return
    }
    void run('Saving…', async signal => {
      const config = await sessionRequest<Config>('/api/config', {
        method: 'PUT', headers: jsonHeaders, body: JSON.stringify(payload), signal,
      })
      if (signal.aborted) return
      setSaved(config)
      setDraft(draftFrom(config))
      setSelectedKey(selected?.id.trim() || config.defaultProvider || config.providers[0]?.id || '')
      if (discovery && !usesSavedConnection(
        draft.providers.find(provider => provider.draftKey === discovery.provider),
        saved.providers.find(provider => provider.id === discovery.provider),
      )) setDiscovery(null)
      setMessage('Provider settings saved.')
      onSaved()
    })
  }

  function discover(provider: Provider) {
    void run('Fetching models…', async signal => {
      const { models } = await sessionRequest<{ models: { id: string }[] }>(
        `/api/providers/${encodeURIComponent(provider.id)}/models`, { method: 'POST', signal }, 45_000,
      )
      if (signal.aborted) return
      const ids = [...new Set(models.map(model => model.id))].sort()
      setDiscovery({ provider: provider.id, models: ids })
      setMessage(ids.length ? `Found ${ids.length} models. Add the ones you want to use, then save.` : 'The provider returned no models.')
    })
  }

  function check(provider: Provider, model: string) {
    if (!window.confirm(`Send a short completion request to ${provider.name} using ${model}? The provider may bill this request.`)) return
    void run('Running paid check…', async signal => {
      const result = await sessionRequest<{ ok: true; model: string; reply: string }>(
        `/api/providers/${encodeURIComponent(provider.id)}/check`,
        { method: 'POST', headers: jsonHeaders, body: JSON.stringify({ model }), signal },
        135_000,
      )
      if (!signal.aborted) setMessage(`Check succeeded for ${result.model}.${result.reply ? ` Reply: ${result.reply}` : ''}`)
    })
  }

  return (
    <section className="panel settings-panel" aria-labelledby="providers-title">
      <div className="panel-header">
        <h2 id="providers-title">API providers</h2>
        <span className="status" data-pending={dirty}>{dirty ? 'Unsaved changes' : 'Saved'}</span>
      </div>
      <fieldset className="settings-body" disabled={Boolean(busy)}>
        <legend className="sr-only">Provider settings</legend>
        <p className="hint">Connect an OpenAI-compatible API. Save the provider before fetching its models or running a check.</p>
        <div className="provider-picker">
          {draft.providers.length > 0 && <label>
            Provider
            <select value={selectedKey} onChange={event => chooseProvider(event.target.value)}>
              {draft.providers.map(provider => (
                <option key={provider.draftKey} value={provider.draftKey}>{provider.name.trim() || provider.id.trim() || 'New provider'}</option>
              ))}
            </select>
          </label>}
          <div className="actions">
            {![...draft.providers, ...saved.providers].some(provider => provider.id.trim() === 'deepseek') &&
              <button type="button" onClick={() => addProvider(true)}>Add DeepSeek</button>}
            <button type="button" onClick={() => addProvider(false)}>Add custom provider</button>
          </div>
        </div>
        {selected && (
          <div className="settings-group">
            <div className="settings-grid">
              <label>Name<input value={selected.name} onChange={event => updateProvider({ name: event.target.value })} /></label>
              <label>Provider ID
                <input value={selected.id} onChange={event => updateProvider({ id: event.target.value })}
                  readOnly={Boolean(savedProvider)} maxLength={48} placeholder="my-provider" autoCapitalize="none" spellCheck={false} />
              </label>
              <label className="settings-wide">API base URL
                <input type="url" value={selected.baseUrl} onChange={event => updateProvider({ baseUrl: event.target.value })}
                  placeholder="https://api.example.com/v1" autoCapitalize="none" spellCheck={false} />
              </label>
              <label>API key environment variable
                <input value={hasPastedKey ? '' : selected.apiKeyEnv ?? ''} disabled={hasPastedKey}
                  onChange={event => updateProvider({ apiKeyEnv: event.target.value })}
                  placeholder="Optional, e.g. PROVIDER_API_KEY" autoCapitalize="none" spellCheck={false} />
              </label>
              <label>API key
                <input type="password" value={selected.apiKey} onChange={event => updateProvider({ apiKey: event.target.value })}
                  placeholder={savedProvider?.hasKey ? 'Configured (leave blank to keep)' : 'Optional if using an environment variable'}
                  autoComplete="new-password" />
              </label>
            </div>
            <p className="hint">
              {savedProvider?.hasKey ? 'A credential is available for the saved provider. ' : savedProvider ? 'No credential is available for the saved provider. ' : ''}
              {hasPastedKey && 'The pasted key will replace the environment variable reference. '}
              Pasted keys are stored locally and never displayed. The key is sent to the configured API base URL.
            </p>
            <label className="checkbox">
              <input type="checkbox" checked={selected.responses} onChange={event => updateProvider({ responses: event.target.checked })} />
              The upstream API supports native Responses
            </label>
            <ProviderModels
              key={selectedKey}
              models={selected.models}
              available={options}
              savedProvider={connectionSaved ? savedProvider : undefined}
              onChange={models => updateProvider({ models })}
              onDiscover={discover}
              onCheck={check}
            />
            <div className="actions">
              <button type="button" className="danger-button" onClick={removeProvider}>Remove provider</button>
            </div>
          </div>
        )}
        {draft.providers.length > 0 && (
          <div className="settings-group">
            <h3>Gateway defaults</h3>
            <div className="settings-grid">
              <label>Default provider
                <select value={draft.defaultProvider ?? ''} onChange={event => setDraft({
                  ...draft, defaultProvider: event.target.value || null, defaultModel: null,
                })}>
                  <option value="">No default provider</option>
                  {draft.providers.map(provider => (
                    <option key={provider.draftKey} value={provider.draftKey}>{provider.name.trim() || provider.id.trim() || 'New provider'}</option>
                  ))}
                </select>
              </label>
              <label>Default model
                <select disabled={!defaultProvider?.models.length} value={draft.defaultModel ?? ''}
                  onChange={event => setDraft({ ...draft, defaultModel: event.target.value || null })}>
                  <option value="">No default model</option>
                  {defaultProvider?.models.map(model => <option key={model}>{model}</option>)}
                </select>
              </label>
            </div>
          </div>
        )}
      </fieldset>
      <div className="panel-footer">
        <button className="primary-button" type="button" disabled={!dirty || Boolean(busy)} onClick={save}>Save settings</button>
        <button type="button" disabled={!dirty || Boolean(busy)} onClick={() => {
          setDraft(draftFrom(saved))
          chooseProvider(saved.providers.some(provider => provider.id === selectedKey)
            ? selectedKey : saved.defaultProvider ?? saved.providers[0]?.id ?? '')
        }}>Discard edits</button>
        {error && <p className="error feedback" role="alert">{error}</p>}
        {(busy || message) && <p className="hint feedback" role="status">{busy || message}</p>}
      </div>
    </section>
  )
}

interface ProviderModelsProps {
  models: string[]
  available?: string[]
  savedProvider?: Provider
  onChange: (models: string[]) => void
  onDiscover: (provider: Provider) => void
  onCheck: (provider: Provider, model: string) => void
}

function ProviderModels({ models, available, savedProvider, onChange, onDiscover, onCheck }: ProviderModelsProps) {
  const [manualModel, setManualModel] = useState('')
  const [discoveredModel, setDiscoveredModel] = useState('')
  const [checkModel, setCheckModel] = useState('')
  const newModels = available?.filter(model => !models.includes(model)) ?? []
  const selectedDiscovered = newModels.includes(discoveredModel) ? discoveredModel : (newModels[0] ?? '')
  const checkOptions = savedProvider?.models ?? []
  const selectedCheck = checkOptions.includes(checkModel) ? checkModel : (checkOptions[0] ?? '')

  function addModel(model: string) {
    const id = model.trim()
    if (id && !models.includes(id)) onChange([...models, id])
  }

  return (
    <div className="settings-group">
      <h3>Models</h3>
      {models.length ? (
        <ul className="model-list">
          {models.map(model => <li key={model}>
            <code>{model}</code>
            <button type="button" aria-label={`Remove model ${model}`} onClick={() => onChange(models.filter(id => id !== model))}>Remove</button>
          </li>)}
        </ul>
      ) : <p className="hint">No models configured.</p>}
      <div className="settings-row">
        <label>Model ID
          <input value={manualModel} onChange={event => setManualModel(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter' && manualModel.trim() && !models.includes(manualModel.trim())) {
                event.preventDefault()
                addModel(manualModel)
                setManualModel('')
              }
            }} placeholder="Type a model ID" autoCapitalize="none" spellCheck={false} />
        </label>
        <button type="button" disabled={!manualModel.trim() || models.includes(manualModel.trim())}
          onClick={() => { addModel(manualModel); setManualModel('') }}>Add model</button>
      </div>
      <div className="actions">
        <button type="button" disabled={!savedProvider} onClick={() => savedProvider && onDiscover(savedProvider)}>Fetch model list</button>
        <span className="hint">{savedProvider ? 'Uses the saved provider. No completion is sent.' : 'Save connection settings to fetch models.'}</span>
      </div>
      {newModels.length > 0 && (
        <div className="settings-row">
          <label>Discovered model
            <select value={selectedDiscovered} onChange={event => setDiscoveredModel(event.target.value)}>
              {newModels.map(model => <option key={model}>{model}</option>)}
            </select>
          </label>
          <button type="button" onClick={() => addModel(selectedDiscovered)}>Add discovered model</button>
        </div>
      )}
      {savedProvider && checkOptions.length > 0 && (
        <div className="settings-row">
          <label>Saved model to check
            <select value={selectedCheck} onChange={event => setCheckModel(event.target.value)}>
              {checkOptions.map(model => <option key={model}>{model}</option>)}
            </select>
          </label>
          <button type="button" onClick={() => onCheck(savedProvider, selectedCheck)}>Run paid chat check</button>
        </div>
      )}
    </div>
  )
}
