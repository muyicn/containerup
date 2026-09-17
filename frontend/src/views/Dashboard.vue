<script setup>
import { onMounted, reactive, ref } from 'vue'
import { store, toast, log, goPage, confirmDialog } from '../store'
import { api } from '../api'
import StatCard from '../components/StatCard.vue'
import Icon from '../components/Icon.vue'

const emit = defineEmits(['unread'])

const s = reactive({
  containers: 0, up_to_date: 0, update_available: 0, ignored: 0,
  unread: 0, projects: {}, latest_job: null, latest_scan: null,
})
const sched = ref(null)
const busy = ref(false)
const activeFilter = ref('')

async function load() {
  Object.assign(s, await api('/summary'))
  sched.value = await api('/scheduler').catch(() => null)
  emit('unread', s.unread)
}

function schedText() {
  if (!sched.value) return '—'
  if (sched.value.busy) return '执行中…'
  return sched.value.enabled ? `每 ${sched.value.interval_sec}s` : '已停用（仅手动）'
}

function nextDueText() {
  if (!sched.value?.enabled || sched.value.busy) return '—'
  if (!sched.value.next_due) return '—'
  const sec = Math.max(0, Math.round((new Date(sched.value.next_due).getTime() - Date.now()) / 1000))
  return `约 ${sec}s 后`
}

function jump(filter) {
  activeFilter.value = filter
  goPage('containers')
  window.dispatchEvent(new CustomEvent('vt-filter', { detail: filter }))
}

async function doScan(force) {
  if (busy.value) return
  busy.value = true
  try {
    log(force ? '强制扫描（无视防抖重播）…' : '立即扫描…', 'info')
    const r = await api('/scan' + (force ? '?force=true' : ''), { method: 'POST' })
    if (r.status === 'ok') {
      toast(`扫描完成：检查 ${r.checked} · 新通知 ${r.events} · 错误 ${r.errors}`)
      log(`扫描完成：checked=${r.checked} events=${r.events} errors=${r.errors} 用时 ${r.duration_ms}ms${r.force ? '（强制）' : ''}`, r.errors ? 'warn' : 'ok')
      for (const e of r.errors_detail || []) log(`检测失败 ${e.name}：${e.error}`, 'err')
    } else {
      toast('扫描进行中，请稍候', true)
      log('扫描互斥：已有扫描在运行', 'warn')
    }
    await load()
  } catch (e) { toast('扫描失败：' + e.message, true) }
  finally { busy.value = false }
}

async function updateAll() {
  const ok = await confirmDialog({
    title: '更新全部有更新的容器？',
    message: '将按 compose 依赖拓扑顺序执行，失败自动回滚。',
    okText: '全部更新',
  })
  if (!ok) return
  try {
    log('全量手动更新开始…', 'info')
    const r = await api('/update?manual=true', { method: 'POST' })
    const ok = r.containers.filter(c => c.result === 'updated').length
    const rb = r.containers.filter(c => c.result === 'rolled_back').length
    toast(`任务 #${r.job_id} 完成：成功 ${ok} · 回滚 ${rb}`)
    log(`更新任务 #${r.job_id}：${r.containers.map(c => `${c.name}=${c.result}`).join(', ')}`, rb ? 'warn' : 'ok')
    await load()
  } catch (e) { toast('更新失败：' + e.message, true) }
}

function scanSummaryText() {
  const sm = s.latest_scan?.summary
  if (!sm || !('checked' in sm)) return '扫描中…'
  return `检查 ${sm.checked} · 新通知 ${sm.events} · 错误 ${sm.errors} · 用时 ${sm.duration_ms}ms${sm.force ? ' · 强制' : ''}`
}

onMounted(load)
defineExpose({ load })
</script>

<template>
  <div class="animate-fade-in">
    <div class="flex items-center justify-between mb-5">
      <h1 class="text-xl font-bold text-slate-900 dark:text-white">仪表盘</h1>
      <span class="text-[11px] text-slate-400 font-mono hidden sm:block">{{ store.demoMode ? 'demo' : 'live' }}</span>
    </div>

    <div class="grid grid-cols-2 xl:grid-cols-4 gap-4">
      <StatCard label="监控容器总数" :value="s.containers" icon="cube" tone="brand" :active="activeFilter === 'all'" @select="jump('all')" />
      <StatCard label="已是最新" :value="s.up_to_date" icon="check" tone="green" :active="activeFilter === 'latest'" @select="jump('latest')" />
      <StatCard label="有更新可用" :value="s.update_available" icon="arrowUp" tone="amber" :active="activeFilter === 'update'" @select="jump('update')" />
      <StatCard label="已忽略" :value="s.ignored" icon="eye" tone="slate" :active="activeFilter === 'ignored'" @select="jump('ignored')" />
    </div>

    <div class="mt-5 flex items-center gap-2.5 flex-wrap">
      <button @click="doScan(false)" class="btn-primary" :disabled="busy">
        <Icon name="scan" cls="w-4 h-4" :class="{ 'animate-spin': busy }" /> 立即扫描
      </button>
      <button @click="doScan(true)" class="btn-warn" title="无视防抖与去重，重播所有差异通知">
        <Icon name="bell" cls="w-4 h-4" /> 强制扫描
      </button>
      <button @click="updateAll()" class="btn-ghost">
        <Icon name="rocket" cls="w-4 h-4" /> 更新全部可用
      </button>
    </div>

    <div class="mt-5 grid md:grid-cols-2 gap-4">
      <div class="card-base p-5">
        <h3 class="flex items-center gap-2 text-sm font-bold text-slate-700 dark:text-slate-200 mb-3">
          <Icon name="layers" cls="w-4 h-4 text-brand-500" /> Compose 项目分布
        </h3>
        <div v-if="Object.keys(s.projects).length" class="space-y-2">
          <div v-for="(count, proj) in s.projects" :key="proj"
            class="flex items-center gap-3 px-3.5 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/60">
            <span class="w-8 h-8 rounded-lg bg-brand-500/10 text-brand-600 dark:text-brand-400 flex items-center justify-center font-bold text-xs">{{ (proj || '?').slice(0, 2).toUpperCase() }}</span>
            <span class="font-medium text-sm text-slate-700 dark:text-slate-200">{{ proj || '独立容器' }}</span>
            <span class="ml-auto text-xs text-slate-400">{{ count }} 个容器</span>
          </div>
        </div>
        <div v-else class="py-8 text-center text-xs text-slate-400">暂无数据，先执行扫描</div>
      </div>

      <div class="card-base p-5">
        <h3 class="flex items-center gap-2 text-sm font-bold text-slate-700 dark:text-slate-200 mb-3">
          <Icon name="clock" cls="w-4 h-4 text-brand-500" /> 最近任务与扫描
        </h3>
        <div class="space-y-2 text-xs">
          <div class="flex items-center gap-2 px-3.5 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/60">
            <Icon name="scan" cls="w-3.5 h-3.5 text-slate-400" />
            <span class="text-slate-500">自动调度</span>
            <span class="ml-auto font-semibold" :class="sched?.busy ? 'text-brand-500' : (sched?.enabled ? 'text-emerald-500' : 'text-slate-400')">
              {{ schedText() }}
            </span>
          </div>
          <div class="flex items-center gap-2 px-3.5 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/60">
            <Icon name="clock" cls="w-3.5 h-3.5 text-slate-400" />
            <span class="text-slate-500">下次自动执行</span>
            <span class="ml-auto text-slate-500 font-mono">{{ nextDueText() }}</span>
          </div>
          <div class="flex items-center gap-2 px-3.5 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/60">
            <Icon name="rocket" cls="w-3.5 h-3.5 text-slate-400" />
            <span class="text-slate-500">最近任务</span>
            <span class="ml-auto" :class="s.latest_job?.status === 'done' ? 'text-emerald-500' : 'text-amber-500'">
              {{ s.latest_job ? `#${s.latest_job.id} · ${s.latest_job.status}` : '暂无' }}
            </span>
          </div>
          <div class="flex items-center gap-2 px-3.5 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/60">
            <Icon name="clock" cls="w-3.5 h-3.5 text-slate-400" />
            <span class="text-slate-500">上次扫描</span>
            <span class="ml-auto text-slate-500 font-mono">{{ s.latest_scan?.finished_at?.slice(0, 19).replace('T', ' ') || '暂未扫描' }}</span>
          </div>
          <div class="px-3.5 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/60 text-slate-500">
            {{ scanSummaryText() }}
            <div v-for="e in s.latest_scan?.summary?.errors_detail || []" :key="e.name"
                 class="mt-1 text-[11px] leading-snug text-rose-500 dark:text-rose-400 break-all">
              检测失败 {{ e.name }}：{{ e.error }}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
