<script setup>
import { onMounted, reactive, ref, computed } from 'vue'
import { store, toast, log, applyTheme, confirmState, resolveConfirm } from './store'
import { api } from './api'
import AppNav from './components/AppNav.vue'
import LogPanel from './components/LogPanel.vue'
import ToastHost from './components/ToastHost.vue'
import Modal from './components/Modal.vue'
import Icon from './components/Icon.vue'
import Logo from './components/Logo.vue'
import Dashboard from './views/Dashboard.vue'
import Containers from './views/Containers.vue'
import Notifications from './views/Notifications.vue'
import Watches from './views/Watches.vue'
import Settings from './views/Settings.vue'

const auth = reactive({ needSetup: false, lockedSec: 0 })
const form = reactive({ username: 'admin', password: '' })
const logging = ref(false)
const unread = ref(0)

const view = computed(() => ({
  dashboard: Dashboard,
  containers: Containers,
  notifications: Notifications,
  watches: Watches,
  settings: Settings,
}[store.page]))

async function authCheck() {
  try {
    const st = await api('/auth/check')
    auth.needSetup = st.need_setup
    auth.lockedSec = st.locked_sec
    if (!st.authenticated) {
      store.showLogin = true
      return
    }
    await enterApp()
  } catch { store.showLogin = true }
}

async function enterApp() {
  store.showLogin = false
  store.authed = true
  try {
    const h = await api('/public/health')
    store.demoMode = h.demo_mode
    store.mockFallback = h.docker_impl === 'mock' && !h.demo_mode
    const s = await api('/summary')
    unread.value = s.unread
  } catch { /* ignore */ }
  log('登录成功，欢迎使用容器守望者', 'ok')
}

async function submit() {
  if (logging.value) return
  logging.value = true
  try {
    await api(auth.needSetup ? '/auth/setup' : '/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: form.username, password: form.password }),
    })
    await enterApp()
  } catch (e) {
    toast(e.message, true)
    authCheck()
  } finally { logging.value = false }
}

function setUnread(n) { unread.value = n }

onMounted(() => {
  applyTheme()
  authCheck()
})
</script>

<template>
  <!-- ============ 登录层 ============ -->
  <Transition name="mask">
    <div v-if="store.showLogin" class="fixed inset-0 z-[80] flex items-center justify-center p-4
         bg-gradient-to-br from-slate-900 via-brand-950 to-brand-800">
      <div class="absolute inset-0 overflow-hidden pointer-events-none">
        <div class="absolute -top-32 -left-32 w-96 h-96 rounded-full bg-brand-500/20 blur-3xl" />
        <div class="absolute -bottom-32 -right-32 w-96 h-96 rounded-full bg-indigo-500/20 blur-3xl" />
      </div>
      <div class="relative w-full max-w-sm rounded-3xl bg-white/95 dark:bg-slate-900/95 backdrop-blur-xl shadow-lift p-8 animate-slide-up">
        <div class="flex flex-col items-center mb-7">
          <Logo class="w-14 h-14 rounded-2xl shadow-glow mb-3" />
          <h1 class="text-xl font-extrabold text-slate-900 dark:text-white tracking-wide">容器守望者</h1>
          <p class="mt-1 text-xs text-slate-400">{{ auth.needSetup ? '首次部署 · 设置管理员密码' : 'Docker 更新守望平台 · 登录' }}</p>
        </div>
        <form @submit.prevent="submit" class="space-y-3.5">
          <input v-model="form.username" class="input" placeholder="用户名" autocomplete="username" />
          <input v-model="form.password" type="password" class="input" placeholder="密码（至少 6 位）" autocomplete="current-password" />
          <button type="submit" class="btn-primary w-full !py-2.5" :disabled="logging || form.password.length < 6">
            <Icon v-if="logging" name="refresh" cls="w-4 h-4 animate-spin" />
            {{ auth.needSetup ? '初始化' : '登录' }}
          </button>
        </form>
        <p v-if="auth.lockedSec > 0" class="mt-3 text-center text-xs text-rose-500">
          失败次数过多，锁定中（剩余 {{ auth.lockedSec }}s）
        </p>
        <p class="mt-4 text-center text-[11px] text-slate-400">演示账号 admin / admin123</p>
      </div>
    </div>
  </Transition>

  <!-- ============ 主应用 ============ -->
  <div v-if="!store.showLogin" class="min-h-screen">
    <AppNav :unread="unread" />
    <main class="lg:pl-60 pt-14 lg:pt-0 pb-24">
      <div class="max-w-[1400px] mx-auto px-4 lg:px-6 py-5 lg:py-7">
        <component :is="view" @unread="setUnread" />
      </div>
    </main>
    <LogPanel />
  </div>

  <ToastHost />

  <!-- ============ 全局现代化确认弹窗（替代原生 confirm） ============ -->
  <Modal :open="confirmState.open" :title="confirmState.title" :tone="confirmState.danger ? 'danger' : 'brand'" @close="resolveConfirm(false)">
    <p class="text-[13.5px] leading-6 text-slate-600 dark:text-slate-300 whitespace-pre-line">{{ confirmState.message }}</p>
    <div class="mt-6 flex justify-end gap-2.5">
      <button @click="resolveConfirm(false)" class="btn-ghost !px-4">{{ confirmState.cancelText }}</button>
      <button @click="resolveConfirm(true)"
        class="btn !px-4 text-white transition-colors active:scale-95"
        :class="confirmState.danger
          ? 'bg-rose-600 hover:bg-rose-700'
          : 'bg-brand-600 hover:bg-brand-700'">
        {{ confirmState.okText }}
      </button>
    </div>
  </Modal>
</template>

<style scoped>
.mask-enter-active, .mask-leave-active { transition: opacity .3s ease; }
.mask-enter-from, .mask-leave-to { opacity: 0; }
</style>
