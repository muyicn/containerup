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
  const text = await res.text()
  let data = {}
  try {
    data = text ? JSON.parse(text) : {}
  } catch {
    data = {}
  }
  if (!res.ok) {
    let errMsg = ''
    if (typeof data.detail === 'string' && data.detail.trim()) {
      errMsg = data.detail.trim()
    } else if (text && !text.trim().startsWith('<')) {
      errMsg = text.slice(0, 150).trim()
    } else {
      errMsg = res.statusText || `HTTP ${res.status}`
    }
    throw new Error(errMsg || '请求处理失败')
  }
  return data
}
