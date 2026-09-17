import { store } from './store'

export async function api(path, opts = {}) {
  const res = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...opts,
  })
  if (res.status === 401) {
    store.showLogin = true
    throw new Error('请先登录')
  }
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : res.statusText)
  return data
}
