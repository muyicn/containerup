<script setup>
import { onMounted, reactive, ref } from 'vue'
import { toast, log, confirmDialog, fmtTime } from '../store'
import { api } from '../api'
import Icon from '../components/Icon.vue'

const rows = ref([])
const form = reactive({ reference: '' })
const busy = ref(false)

async function load() { rows.value = await api('/watches') }

async function add() {
  if (!form.reference.trim()) return
  busy.value = true
  try {
    await api('/watches', { method: 'POST', body: JSON.stringify({ reference: form.reference.trim() }) })
    toast('监控项已添加，下轮扫描建立基线')
    log(`添加远端监控：${form.reference.trim()}`)
    form.reference = ''
    await load()
  } catch (e) { toast(e.message, true) }
  finally { busy.value = false }
}

async function del(id, ref) {
  const ok = await confirmDialog({
    title: `删除监控 ${ref}？`,
    message: '删除后不再检测该镜像的版本发布。',
    danger: true,
    okText: '删除',
  })
  if (!ok) return
  try { await api(`/watches/${id}`, { method: 'DELETE' }); log(`删除远端监控：${ref}`); await load() } catch (e) { toast(e.message, true) }
}

function statusOf(w) {
  if (!w.last_checked_at) return { t: '未检查', cls: 'bg-slate-500/10 text-slate-400' }
  if (w.baseline_digest && w.remote_digest && w.baseline_digest !== w.remote_digest)
    return { t: '上游已更新', cls: 'bg-amber-500/10 text-amber-600 dark:text-amber-400' }
  return { t: '基线一致', cls: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' }
}

function modeOf(w) {
  return w.effective_mode === 'pin-watch'
    ? { t: '版本追踪', cls: 'bg-sky-500/10 text-sky-600 dark:text-sky-400' }
    : { t: '摘要追踪', cls: 'bg-violet-500/10 text-violet-600 dark:text-violet-400' }
}

onMounted(load)
</script>

<template>
  <div class="animate-fade-in">
    <h1 class="text-xl font-bold text-slate-900 dark:text-white mb-1">远端镜像监控</h1>
    <p class="text-xs text-slate-400 mb-5">无需本机运行，直接盯住任意 registry 镜像；上游出现新摘要或新版本 tag 时推送通知</p>

    <div class="card-base p-5">
      <div class="flex gap-2">
        <input v-model="form.reference" @keyup.enter="add" class="input font-mono" placeholder="镜像引用，如 nginx:latest 或 myapp:2.0" />
        <button @click="add()" class="btn-primary shrink-0" :disabled="busy || !form.reference.trim()">
          <Icon name="eye" cls="w-4 h-4" /> 添加
        </button>
      </div>

      <div v-if="rows.length" class="mt-4 space-y-2.5">
        <div v-for="w in rows" :key="w.id"
          class="px-3.5 py-3 rounded-xl bg-slate-50 dark:bg-slate-800/60 animate-fade-in">
          <div class="flex items-center gap-2.5 flex-wrap">
            <Icon name="eye" cls="w-4 h-4 text-brand-500 shrink-0" />
            <span class="font-mono text-[13px] text-slate-700 dark:text-slate-200 truncate">{{ w.reference }}</span>
            <span class="badge shrink-0" :class="modeOf(w).cls">{{ modeOf(w).t }}</span>
            <span class="badge shrink-0 ml-auto" :class="statusOf(w).cls">{{ statusOf(w).t }}</span>
            <button @click="del(w.id, w.reference)" class="btn-danger !px-2.5 !py-1 !text-[11px] shrink-0">删除</button>
          </div>
          <div class="mt-1.5 pl-7 flex items-center gap-3 flex-wrap text-[11px] text-slate-400 font-mono">
            <span v-if="w.newer_tags?.length" class="text-sky-600 dark:text-sky-400 font-sans">
              可用新版本：{{ w.newer_tags.join('、') }}
            </span>
            <span v-if="w.last_checked_at">最近检查 {{ fmtTime(w.last_checked_at) }}</span>
            <span v-else>等待下轮扫描</span>
          </div>
        </div>
      </div>
      <div v-else class="mt-4 py-8 text-center text-xs text-slate-400">暂无监控项</div>
    </div>
  </div>
</template>
