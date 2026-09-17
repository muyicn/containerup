<script setup>
import { computed } from 'vue'
import { store, goPage, toggleTheme, log } from '../store'
import { api } from '../api'
import Icon from './Icon.vue'
import Logo from './Logo.vue'

const props = defineProps({
  unread: { type: Number, default: 0 },
})

const items = [
  { id: 'dashboard', label: '仪表盘', icon: 'dashboard' },
  { id: 'containers', label: '容器', icon: 'cube' },
  { id: 'notifications', label: '通知', icon: 'bell' },
  { id: 'watches', label: '远端监控', icon: 'eye' },
  { id: 'settings', label: '设置', icon: 'gear' },
]

const isDark = computed(() => store.theme === 'dark')

async function logout() {
  try {
    await api('/auth/logout', { method: 'POST' })
    log('已退出登录')
    location.reload()
  } catch { /* ignore */ }
}

function pick(item) {
  goPage(item.id)
}
</script>

<template>
  <!-- ============ 桌面侧边栏 ============ -->
  <aside
    class="hidden lg:flex fixed inset-y-0 left-0 z-40 w-60 flex-col
           bg-white/70 dark:bg-slate-900/70 backdrop-blur-xl
           border-r border-slate-200/70 dark:border-slate-700/60"
  >
    <!-- Logo -->
    <div class="flex items-center gap-2.5 px-5 pt-6 pb-5">
      <Logo class="w-9 h-9 rounded-xl shadow-glow shrink-0" />
      <div class="leading-tight">
        <div class="font-bold text-[15px] tracking-wide text-slate-900 dark:text-white">容器守望者</div>
        <div class="text-[10.5px] text-slate-400">Docker 更新守望平台</div>
      </div>
    </div>

    <!-- 导航 -->
    <nav class="flex-1 px-3 space-y-1 overflow-y-auto">
      <button
        v-for="it in items" :key="it.id"
        @click="pick(it)"
        class="w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl text-sm font-medium
               transition-all duration-200 group"
        :class="store.page === it.id
          ? 'bg-gradient-to-r from-brand-600 to-brand-500 text-white shadow-sm'
          : 'text-slate-500 dark:text-slate-400 hover:bg-slate-100/80 dark:hover:bg-slate-800/60 hover:text-slate-800 dark:hover:text-slate-200'"
      >
        <Icon :name="it.icon" cls="w-[18px] h-[18px]" />
        <span>{{ it.label }}</span>
        <span
          v-if="it.id === 'notifications' && unread > 0"
          class="ml-auto min-w-[20px] h-5 px-1.5 rounded-full text-[11px] font-bold flex items-center justify-center"
          :class="store.page === it.id ? 'bg-white/25 text-white' : 'bg-rose-500 text-white'"
        >{{ unread > 99 ? '99+' : unread }}</span>
      </button>
    </nav>

    <!-- 底部：状态标签 + 账号操作（整齐两列 grid，修复错位） -->
    <div class="px-3 pb-4 space-y-2">
      <div v-if="store.demoMode || store.mockFallback"
        class="mx-1 px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-500/10 border border-amber-200/60 dark:border-amber-500/20
               text-[11px] font-semibold text-amber-600 dark:text-amber-400 text-center">
        {{ store.demoMode ? 'DEMO 模式 · 内存 Mock' : '无 Docker 引擎 · Mock 模式' }}
      </div>
      <div class="mx-1 pt-2 border-t border-slate-200/70 dark:border-slate-700/60 grid grid-cols-2 gap-2">
        <button @click="logout"
          class="flex items-center justify-center gap-1.5 py-2 rounded-lg text-xs font-medium text-slate-500 dark:text-slate-400
                 hover:bg-rose-50 dark:hover:bg-rose-500/10 hover:text-rose-600 dark:hover:text-rose-400 transition-colors duration-200">
          <Icon name="logout" cls="w-4 h-4" /> 退出
        </button>
        <button @click="toggleTheme"
          class="flex items-center justify-center gap-1.5 py-2 rounded-lg text-xs font-medium text-slate-500 dark:text-slate-400
                 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-slate-800 dark:hover:text-slate-200 transition-colors duration-200">
          <Icon :name="isDark ? 'sun' : 'moon'" cls="w-4 h-4" /> {{ isDark ? '浅色' : '深色' }}
        </button>
      </div>
    </div>
  </aside>

  <!-- ============ 移动端顶栏 ============ -->
  <header
    class="lg:hidden fixed top-0 inset-x-0 z-40 h-14 flex items-center gap-3 px-4
           bg-white/75 dark:bg-slate-900/75 backdrop-blur-xl border-b border-slate-200/70 dark:border-slate-700/60"
  >
    <button @click="store.drawerOpen = true"
      class="p-2 -ml-2 rounded-lg text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
      aria-label="打开菜单">
      <Icon name="menu" cls="w-6 h-6" />
    </button>
    <div class="flex items-center gap-2">
      <Logo class="w-7 h-7 rounded-lg" />
      <span class="font-bold text-sm text-slate-900 dark:text-white">容器守望者</span>
    </div>
    <button @click="toggleTheme" class="ml-auto p-2 rounded-lg text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors" aria-label="切换主题">
      <Icon :name="isDark ? 'sun' : 'moon'" cls="w-5 h-5" />
    </button>
  </header>

  <!-- ============ 移动端抽屉 ============ -->
  <Teleport to="body">
    <Transition name="drawer-fade">
      <div v-if="store.drawerOpen" class="lg:hidden fixed inset-0 z-50 bg-slate-900/40 backdrop-blur-sm" @click="store.drawerOpen = false" />
    </Transition>
    <Transition name="drawer">
      <aside v-if="store.drawerOpen"
        class="lg:hidden fixed inset-y-0 left-0 z-50 w-64 flex flex-col
               bg-white dark:bg-slate-900 shadow-lift">
        <div class="flex items-center gap-2.5 px-5 pt-6 pb-5">
          <Logo class="w-9 h-9 rounded-xl shadow-glow shrink-0" />
          <div class="leading-tight">
            <div class="font-bold text-[15px] text-slate-900 dark:text-white">容器守望者</div>
            <div class="text-[10.5px] text-slate-400">Docker 更新守望平台</div>
          </div>
          <button @click="store.drawerOpen = false" class="ml-auto p-1.5 rounded-lg text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800">
            <Icon name="x" cls="w-5 h-5" />
          </button>
        </div>
        <nav class="flex-1 px-3 space-y-1 overflow-y-auto">
          <button v-for="it in items" :key="it.id" @click="pick(it)"
            class="w-full flex items-center gap-3 px-3.5 py-3 rounded-xl text-sm font-medium transition-all duration-200"
            :class="store.page === it.id
              ? 'bg-gradient-to-r from-brand-600 to-brand-500 text-white shadow-sm'
              : 'text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800'">
            <Icon :name="it.icon" cls="w-[18px] h-[18px]" />
            <span>{{ it.label }}</span>
            <span v-if="it.id === 'notifications' && unread > 0"
              class="ml-auto min-w-[20px] h-5 px-1.5 rounded-full bg-rose-500 text-white text-[11px] font-bold flex items-center justify-center">{{ unread }}</span>
          </button>
        </nav>
        <div class="px-5 pb-6">
          <div v-if="store.demoMode || store.mockFallback"
            class="mb-3 px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-500/10 border border-amber-200/60 text-[11px] font-semibold text-amber-600 text-center">
            {{ store.demoMode ? 'DEMO 模式' : 'Mock 模式' }}
          </div>
          <button @click="logout"
            class="w-full flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-medium
                   bg-rose-50 dark:bg-rose-500/10 text-rose-600 dark:text-rose-400 hover:bg-rose-100 dark:hover:bg-rose-500/20 transition-colors">
            <Icon name="logout" cls="w-4 h-4" /> 退出登录
          </button>
        </div>
      </aside>
    </Transition>
  </Teleport>
</template>

<style scoped>
.drawer-enter-active, .drawer-leave-active { transition: transform .3s cubic-bezier(.16,1,.3,1); }
.drawer-enter-from, .drawer-leave-to { transform: translateX(-100%); }
.drawer-fade-enter-active, .drawer-fade-leave-active { transition: opacity .25s ease; }
.drawer-fade-enter-from, .drawer-fade-leave-to { opacity: 0; }
</style>
