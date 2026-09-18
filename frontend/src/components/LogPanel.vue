<script setup>
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { store, fmtTimeShort } from '../store'
import { api } from '../api'
import Icon from './Icon.vue'

const listEl = ref(null)
const serverLogs = ref([])
let pollTimer = null

async function loadServerLogs() {
  try {
    serverLogs.value = await api('/logs?limit=200')
  } catch { /* 未登录/网络错误时静默，仅显示会话日志 */ }
}

watch(() => store.logOpen, async (open) => {
  if (open) {
    await loadServerLogs()
    await nextTick()
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
    if (!pollTimer) pollTimer = setInterval(loadServerLogs, 5000)
  } else if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
})

onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })

// 合并后端持久日志 + 本地会话日志，按时间排序（同条目去重：ts+msg）
const merged = computed(() => {
  const seen = new Set()
  const all = []
  for (const l of serverLogs.value) {
    const key = `${l.ts}|${l.msg}`
    if (seen.has(key)) continue
    seen.add(key)
    all.push({ ts: l.ts, level: l.level, msg: l.msg })
  }
  for (const l of store.logs) {
    const key = `${l.ts}|${l.msg}`
    if (seen.has(key)) continue
    seen.add(key)
    all.push({ ts: l.ts, level: l.level, msg: l.msg })
  }
  return all.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0))
})

watch(() => merged.value.length, async () => {
  if (store.logOpen) {
    await nextTick()
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  }
})

const levelCls = {
  info: 'text-brand-500',
  ok: 'text-emerald-500',
  warn: 'text-amber-500',
  err: 'text-rose-500',
}
const levelDot = {
  info: 'bg-brand-400',
  ok: 'bg-emerald-400',
  warn: 'bg-amber-400',
  err: 'bg-rose-400',
}
</script>

<template>
  <!-- 底部折叠活动日志面板（容器不拦截点击，仅面板与按钮本身可交互；lg 以上避开侧边栏） -->
  <div class="fixed bottom-0 inset-x-0 lg:left-60 z-40 pointer-events-none">
    <div class="max-w-[1400px] mx-auto px-3 lg:px-6 pb-3 flex flex-col items-center pointer-events-none">
      <!-- 展开态面板 -->
      <Transition name="log">
        <div v-if="store.logOpen"
          class="mb-2 w-full card-base !rounded-2xl overflow-hidden shadow-lift animate-slide-up
                 bg-white/95 dark:bg-slate-900/95 backdrop-blur-xl pointer-events-auto">
          <div class="flex items-center gap-2 px-4 h-10 border-b border-slate-200/70 dark:border-slate-700/60">
            <Icon name="terminal" cls="w-4 h-4 text-brand-500" />
            <span class="text-xs font-semibold text-slate-600 dark:text-slate-300">活动日志</span>
            <span class="text-[10px] text-slate-400">（发现更新 / 任务执行 / 回滚全流程，本地时区显示）</span>
            <span class="flex-1"></span>
            <span v-if="serverLogs.length" class="text-[10px] text-slate-400 font-mono">持久 {{ serverLogs.length }} 条 · 5s 刷新</span>
            <button v-if="store.logs.length" @click="store.logs = []"
              class="text-[11px] text-slate-400 hover:text-rose-500 transition-colors">清空本会话</button>
            <button @click="store.logOpen = false"
              class="p-1 rounded text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors">
              <Icon name="x" cls="w-4 h-4" />
            </button>
          </div>
          <div ref="listEl" class="max-h-44 overflow-y-auto px-4 py-2.5 space-y-1 font-mono text-[11.5px] leading-5">
            <div v-if="!merged.length" class="text-slate-400 text-center py-4">暂无日志，执行扫描或更新操作后在此查看</div>
            <div v-for="(l, i) in merged" :key="i" class="flex items-start gap-2 animate-fade-in">
              <span class="text-slate-400 shrink-0" :title="l.msg">{{ fmtTimeShort(l.ts) }}</span>
              <span class="w-1.5 h-1.5 rounded-full mt-[7px] shrink-0" :class="levelDot[l.level]" />
              <span :class="levelCls[l.level]" class="break-all">{{ l.msg }}</span>
            </div>
          </div>
        </div>
      </Transition>

      <!-- 收起态触发条 -->
      <button @click="store.logOpen = !store.logOpen"
        class="pointer-events-auto flex items-center gap-2 h-8 px-4 rounded-t-xl
               bg-white/85 dark:bg-slate-800/85 backdrop-blur border border-b-0 border-slate-200/70 dark:border-slate-700/60
               text-[11.5px] font-medium text-slate-500 dark:text-slate-400
               hover:text-brand-600 dark:hover:text-brand-400 transition-colors shadow-soft">
        <Icon name="terminal" cls="w-3.5 h-3.5" />
        活动日志
        <span v-if="merged.length" class="px-1.5 rounded-full bg-brand-500/15 text-brand-600 dark:text-brand-400 text-[10px] font-bold">{{ merged.length }}</span>
        <Icon name="arrowRight" cls="w-3 h-3 transition-transform duration-300" :class="store.logOpen ? 'rotate-90' : 'rotate-90 scale-y-[-1]'" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.log-enter-active, .log-leave-active { transition: all .3s cubic-bezier(.16,1,.3,1); }
.log-enter-from, .log-leave-to { opacity: 0; transform: translateY(16px); }
</style>
