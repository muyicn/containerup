import { reactive } from 'vue'

export const store = reactive({
  page: 'dashboard',
  theme: localStorage.getItem('vt-theme') || 'light',
  showLogin: true,
  demoMode: false,
  mockFallback: false,
  drawerOpen: false,   // 移动端抽屉
  toasts: [],
  logs: [],            // 活动日志（底部面板）
  logOpen: false,
  busy: false,
})

let toastId = 0
export function toast(msg, isErr = false) {
  const id = ++toastId
  store.toasts.push({ id, msg, isErr })
  setTimeout(() => { store.toasts = store.toasts.filter(t => t.id !== id) }, 3400)
}

// ---------- 全局确认弹窗（替代原生 confirm） ----------
export const confirmState = reactive({
  open: false,
  title: '',
  message: '',
  danger: false,
  okText: '确定',
  cancelText: '取消',
  resolve: null,
})

/**
 * 现代化确认弹窗，用法与原生 confirm 一致：
 *   if (await confirmDialog({ title, message })) { ... }
 */
export function confirmDialog({ title = '确认操作', message = '', danger = false, okText = '确定', cancelText = '取消' } = {}) {
  return new Promise((resolve) => {
    Object.assign(confirmState, { open: true, title, message, danger, okText, cancelText, resolve })
  })
}

export function resolveConfirm(val) {
  confirmState.open = false
  confirmState.resolve?.(val)
  confirmState.resolve = null
}

export function log(msg, level = 'info') {
  store.logs.push({
    time: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
    msg, level,
  })
  if (store.logs.length > 300) store.logs.shift()
}

export function applyTheme() {
  document.documentElement.classList.toggle('dark', store.theme === 'dark')
  localStorage.setItem('vt-theme', store.theme)
}

export function toggleTheme() {
  store.theme = store.theme === 'dark' ? 'light' : 'dark'
  applyTheme()
  log(`主题切换 → ${store.theme === 'dark' ? '深色' : '浅色'}`)
}

export function goPage(p) {
  store.page = p
  store.drawerOpen = false
}
