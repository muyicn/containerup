<script setup>
import { onMounted, onUnmounted, ref, computed } from 'vue'
import { store, toast, log } from '../store'
import { api } from '../api'
import ContainerCard from '../components/ContainerCard.vue'
import Icon from '../components/Icon.vue'

const containers = ref([])
const filter = ref('all')
const keyword = ref('')
const loading = ref(false)
const schedEnabled = ref(true)
const hasAutoContainers = computed(() => containers.value.some(c => c.update_enabled && !c.ignored && !c.protected))

const FILTERS = [
  { v: 'all', t: '全部' },
  { v: 'update', t: '有更新' },
  { v: 'behind', t: '有新版可选' },
  { v: 'latest', t: '已最新' },
  { v: 'ignored', t: '已忽略' },
]

const shown = computed(() => {
  let list = containers.value
  if (filter.value === 'update') list = list.filter(c => c.update_available)
  if (filter.value === 'behind') list = list.filter(c => !c.update_available && c.latest_tag && c.latest_tag !== c.cur_tag)
  if (filter.value === 'latest') list = list.filter(c => !c.update_available && !(c.latest_tag && c.latest_tag !== c.cur_tag) && !c.ignored)
  if (filter.value === 'ignored') list = list.filter(c => c.ignored)
  const kw = keyword.value.trim().toLowerCase()
  if (kw) list = list.filter(c => (c.name + c.image_spec).toLowerCase().includes(kw))
  return list
})

async function load() {
  loading.value = true
  try {
    containers.value = await api('/containers')
    const sched = await api('/scheduler').catch(() => null)
    if (sched) schedEnabled.value = !!sched.enabled
  } catch (e) { toast(e.message, true) }
  finally { loading.value = false }
}

function onVtFilter(e) { filter.value = e.detail || 'all' }

onMounted(() => {
  window.addEventListener('vt-filter', onVtFilter)
  load()
})
onUnmounted(() => window.removeEventListener('vt-filter', onVtFilter))
</script>

<template>
  <div class="animate-fade-in">
    <div class="flex items-center justify-between mb-5">
      <h1 class="text-xl font-bold text-slate-900 dark:text-white">容器
        <span class="ml-1 text-sm font-normal text-slate-400">{{ shown.length }} / {{ containers.length }}</span>
      </h1>
      <button @click="load()" class="btn-ghost !px-3 !py-1.5 !text-xs" :disabled="loading">
        <Icon name="refresh" cls="w-3.5 h-3.5" :class="{ 'animate-spin': loading }" /> 刷新
      </button>
    </div>

    <!-- 工具条 -->
    <div class="flex items-center gap-2 flex-wrap mb-3">
      <button v-for="f in FILTERS" :key="f.v" @click="filter = f.v" :class="filter === f.v ? 'chip-on' : 'chip'">{{ f.t }}</button>
      <span class="flex-1 min-w-[140px] max-w-xs relative">
        <Icon name="eye" cls="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" />
        <input v-model="keyword" class="input !pl-9" placeholder="搜索容器 / 镜像…" />
      </span>
    </div>

    <!-- 调度停用提示：勾了「自动」但全局自动调度未开启 → 自动更新不会执行 -->
    <div v-if="!schedEnabled && hasAutoContainers"
      class="mb-5 px-4 py-2.5 rounded-xl bg-amber-50 dark:bg-amber-500/10 border border-amber-200/60 dark:border-amber-500/20 text-xs text-amber-600 dark:text-amber-400">
      已有容器开启「自动」开关，但全局自动调度未开启 —— 自动更新不会执行。请到「设置」页将自动检测间隔设为大于 0（如 3600 秒）。
    </div>

    <!-- 卡片流：统一列高，卡片内部 flex 保证操作区贴底 -->
    <div v-if="shown.length" class="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(340px,1fr))]">
      <ContainerCard v-for="c in shown" :key="c.name" :c="c" :demo="store.demoMode || store.mockFallback" @refresh="load" class="animate-slide-up" />
    </div>
    <div v-else class="card-base py-16 text-center text-sm text-slate-400">
      {{ loading ? '加载中…' : '没有匹配的容器' }}
    </div>
  </div>
</template>
