<script setup>
import { onMounted, ref, computed } from 'vue'
import { toast, log, confirmDialog } from '../store'
import { api } from '../api'
import Icon from '../components/Icon.vue'

const emit = defineEmits(['unread'])

const rows = ref([])
const ntype = ref('all')
const loading = ref(false)

const TABS = [
  { v: 'all', t: '全部' },
  { v: 'update', t: '有新版本' },
  { v: 'new-tag', t: '可选更新' },
  { v: 'job', t: '任务结果' },
]

const shown = computed(() => ntype.value === 'all' ? rows.value : rows.value.filter(r => r.type === ntype.value))

async function load() {
  loading.value = true
  try {
    rows.value = await api('/notifications')
    emit('unread', rows.value.filter(r => !r.read_at).length)
  } catch (e) { toast(e.message, true) }
  finally { loading.value = false }
}

function descOf(n) {
  const p = n.payload || {}
  if (n.type === 'update') {
    if (p.watch) return `🔭 远端监控 ${p.reference || n.target}：上游镜像内容更新`
    return p.project
      ? `📦 项目 ${p.project}：${(p.services || []).length} 个服务有新版本 — ${(p.services || []).map(s => s.target).join('、')}`
      : `🐳 ${n.target} 镜像内容更新`
  }
  if (n.type === 'new-tag') {
    if (p.watch) return `🔭 远端监控 ${p.reference || n.target}：发现新版本 ${p.tag || ''}`
    const tags = (p.services || []).map(s => s.tag).filter(Boolean)
    const targets = (p.services || []).map(s => s.target)
    return `⬆️ ${p.project || n.target} 发现更高版本：${tags.join('、')}（当前 ${targets.join('、')}）`
  }
  const rolled = (p.rolled_back || []).join('、')
  return `📋 任务 #${p.job_id}：成功 ${(p.updated || []).length} · 回滚 ${(p.rolled_back || []).length} · 失败 ${(p.failed || []).length}${rolled ? `（回滚：${rolled}）` : ''}`
}

const typeMeta = {
  'update': { t: '有新版本', cls: 'bg-amber-500/10 text-amber-600 dark:text-amber-400' },
  'new-tag': { t: '可选更新', cls: 'bg-sky-500/10 text-sky-600 dark:text-sky-400' },
  'job': { t: '任务结果', cls: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' },
}

async function readOne(id) {
  try { await api(`/notifications/${id}/read`, { method: 'POST' }); await load() } catch (e) { toast(e.message, true) }
}
async function readAll() {
  try { await api('/notifications/read-all', { method: 'POST' }); log('全部通知标记已读'); await load() } catch (e) { toast(e.message, true) }
}
async function clearRead() {
  const ok = await confirmDialog({
    title: '清空全部已读通知？',
    message: '未读通知不受影响，此操作不可撤销。',
    danger: true,
    okText: '清空',
  })
  if (!ok) return
  try { await api('/notifications/clear-read', { method: 'POST' }); log('已清空全部已读通知'); await load() } catch (e) { toast(e.message, true) }
}

onMounted(load)
</script>

<template>
  <div class="animate-fade-in">
    <div class="flex items-center justify-between mb-5">
      <h1 class="text-xl font-bold text-slate-900 dark:text-white">通知中心</h1>
      <div class="flex gap-2">
        <button @click="readAll()" class="btn-ghost !px-3 !py-1.5 !text-xs">全部已读</button>
        <button @click="clearRead()" class="btn-ghost !px-3 !py-1.5 !text-xs">清空已读</button>
      </div>
    </div>

    <div class="flex gap-2 flex-wrap mb-5">
      <button v-for="t in TABS" :key="t.v" @click="ntype = t.v" :class="ntype === t.v ? 'chip-on' : 'chip'">{{ t.t }}</button>
    </div>

    <div v-if="shown.length" class="space-y-2.5">
      <div v-for="n in shown" :key="n.id"
        class="card-base p-4 flex items-start gap-3.5 transition-all duration-200 hover:-translate-y-0.5 animate-slide-up"
        :class="n.read_at ? '' : '!border-l-[3px] !border-l-brand-500'">
        <span class="badge shrink-0 mt-0.5" :class="typeMeta[n.type]?.cls">{{ typeMeta[n.type]?.t || n.type }}</span>
        <div class="min-w-0 flex-1">
          <p class="text-[13.5px] leading-6 text-slate-700 dark:text-slate-200 break-all">{{ descOf(n) }}</p>
          <p class="mt-1 text-[11px] text-slate-400 font-mono">{{ n.created_at?.slice(0, 19).replace('T', ' ') }}</p>
        </div>
        <button v-if="!n.read_at" @click="readOne(n.id)" class="btn-ghost !px-2.5 !py-1 !text-[11px] shrink-0">标记已读</button>
        <Icon v-else name="check" cls="w-4 h-4 text-emerald-400 shrink-0 mt-1" />
      </div>
    </div>
    <div v-else class="card-base py-16 text-center text-sm text-slate-400">
      {{ loading ? '加载中…' : '暂无通知' }}
    </div>
  </div>
</template>
