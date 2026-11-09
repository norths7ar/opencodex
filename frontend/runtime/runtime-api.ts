export async function request<T>(path: string, options?: RequestInit, timeoutMs = 3000): Promise<T> {
  const timeout = AbortSignal.timeout(timeoutMs)
  const response = await fetch(path, {
    ...options,
    signal: options?.signal ? AbortSignal.any([options.signal, timeout]) : timeout,
    cache: 'no-store',
    redirect: 'error',
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = body?.error?.message ?? body?.error ?? body?.detail
    const validation = Array.isArray(detail) ? detail.flatMap(issue =>
      typeof issue?.msg === 'string' ? [issue.msg.replace(/^Value error, /, '')] : [],
    ).join(' ') : ''
    throw new Error(typeof detail === 'string' ? detail : validation || `Request failed (HTTP ${response.status}).`)
  }
  return response.json() as Promise<T>
}

export async function sessionToken(signal?: AbortSignal) {
  const { token } = await request<{ token: string }>('/opencodex-session', { signal })
  if (typeof token !== 'string' || !token) throw new Error('No session token was returned.')
  return token
}

export async function sessionRequest<T>(path: string, options?: RequestInit, timeoutMs?: number) {
  const token = await sessionToken(options?.signal ?? undefined)
  const headers = new Headers(options?.headers)
  headers.set('Authorization', `Bearer ${token}`)
  return request<T>(path, { ...options, headers }, timeoutMs)
}

export function errorMessage(failure: unknown) {
  if (failure instanceof DOMException && failure.name === 'TimeoutError') return 'The request timed out.'
  return failure instanceof Error ? failure.message : 'The request failed.'
}
