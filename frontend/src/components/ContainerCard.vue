<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api'
import { toast, log, confirmDialog, fmtTime } from '../store'
import Icon from './Icon.vue'
import Modal from './Modal.vue'

const props = defineProps({
  c: { type: Object, required: true },
  demo: { type: Boolean, default: false },
})
const emit = defineEmits(['refresh'])

const menuOpen = ref(false)
const menuRef = ref(null)

// 菜单：点击外部 / Esc 关闭（修复"固定住"问题）
function onDocDown(e) {
  if (menuOpen.value && menuRef.value && !menuRef.value.contains(e.target)) {
    menuOpen.value = false
  }
}
function onEsc() { menuOpen.value = false }
onMounted(() => {
  document.addEventListener('mousedown', onDocDown)
  document.addEventListener('keydown', onEsc)
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocDown)
  document.removeEventListener('keydown', onEsc)
})

const tagUpgrade = computed(() => props.c.latest_tag && props.c.latest_tag !== props.c.cur_tag)
const hasNewer = computed(() => (props.c.newer_tags || []).length > 0)

const status = computed(() => {
  if (props.c.ignored) return 'ignored'
  if (props.c.update_available) return 'update'
  if (tagUpgrade.value) return 'behind'   // 锁定版但仓库有更高 tag（仅弱提醒，不强制）
  return 'latest'
})

// 状态语义统一映射：色条 / 徽章 全部引用同一份
const statusMeta = {
  latest:  { label: '最新',       bar: 'bg-emerald-500', badge: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400' },
  update:  { label: '有更新',     bar: 'bg-amber-500',   badge: 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-400' },
  behind:  { label: '有新版可选', bar: 'bg-sky-500',     badge: 'bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-400' },
  ignored: { label: '已忽略',     bar: 'bg-slate-400',   badge: 'bg-slate-100 text-slate-500 dark:bg-slate-500/10 dark:text-slate-400' },
}

const MODES = [
  { v: 'auto', t: 'Auto' },
  { v: 'digest-only', t: 'Digest' },
  { v: 'pin-watch', t: 'Pin' },
]
const modeLabel = computed(() => MODES.find(m => m.v === props.c.mode)?.t || 'Auto')

// 实际生效状态说明：模式 × 自动开关 → 一句话讲清行为（问题 3）
const effectHint = computed(() => {
  if (props.c.ignored) {
    return { icon: 'eye', text: '已忽略 — 不再检测与更新', cls: 'text-slate-400' }
  }
  if (props.c.local_image) {
    return { icon: 'cube', text: '本地镜像 — 无远端仓库记录，跳过更新检测（镜像推送后自动恢复）', cls: 'text-slate-400' }
  }
  if (props.c.protected) {
    return { icon: 'lock', text: '受保护 — 平台不自动更新，仅提醒', cls: 'text-sky-500' }
  }
  const auto = !!props.c.update_enabled
  const mode = props.c.mode
  if (!auto) {
    return { icon: 'bell', text: `${modeLabel.value} 检测 · 仅通知 — 发现更新只提醒，需手动点「更新」`, cls: 'text-sky-500' }
  }
  if (mode === 'pin-watch') {
    return { icon: 'lock', text: '锁定当前 tag 自动同步内容；更高版本仅提醒，不自动升级', cls: 'text-emerald-500' }
  }
  if (mode === 'digest-only') {
    return { icon: 'bolt', text: '跟踪当前 tag 内容变化，一有更新即自动重建', cls: 'text-emerald-500' }
  }
  return { icon: 'bolt', text: 'Auto · 自动更新 — 当前 tag 有新内容即自动重建', cls: 'text-emerald-500' }
})

async function setMode(m) {
  try {
    await api(`/containers/${props.c.name}/mode`, { method: 'PUT', body: JSON.stringify({ mode: m }) })
    toast(`${props.c.name} 模式 → ${m}`)
    log(`容器 ${props.c.name} 模式切换为 ${m}`)
    emit('refresh')
  } catch (e) { toast(e.message, true) }
}

async function toggleUpdate(v) {
  try {
    await api(`/containers/${props.c.name}/update_enabled`, { method: 'PUT', body: JSON.stringify({ value: v }) })
    log(`容器 ${props.c.name} 自动更新 ${v ? '开启' : '关闭'}`)
    emit('refresh')
  } catch (e) { toast(e.message, true) }
}

async function toggleIgnore(v) {
  try {
    await api(`/containers/${props.c.name}/ignored`, { method: 'PUT', body: JSON.stringify({ value: v }) })
    toast(v ? `${props.c.name} 已忽略` : `${props.c.name} 已恢复`)
    log(`容器 ${props.c.name} ${v ? '设为忽略' : '恢复监控'}`, v ? 'warn' : 'info')
    emit('refresh')
  } catch (e) { toast(e.message, true) }
}

async function update() {
  const ok = await confirmDialog({
    title: `更新 ${props.c.name}？`,
    message: '相关联容器将按依赖顺序处理，失败自动回滚。',
    okText: '更新',
  })
  if (!ok) return
  try {
    log(`手动更新 ${props.c.name} …`)
    const r = await api(`/containers/${props.c.name}/update`, { method: 'POST' })
    if (r.status === 'update already running') {
      toast('已有更新任务在执行中，请稍后重试', true)
      log('更新让行：已有更新任务正在执行中', 'warn')
      return
    }
    const containers = r.containers || []
    const mine = containers.find(x => x.name === props.c.name)
    toast(`任务 #${r.job_id}：${props.c.name} → ${mine ? mine.result : '已执行'}`)
    log(`更新任务 #${r.job_id}：${containers.map(x => `${x.name}=${x.result}`).join(', ')}`, mine?.result === 'rolled_back' ? 'warn' : 'ok')
    emit('refresh')
  } catch (e) {
    const errText = e.message || '未知错误'
    toast('更新失败：' + errText, true)
    log(`更新失败：${errText}`, 'err')
  }
}

async function simFail() {
  try {
    await api('/demo/simulate-failure', { method: 'POST', body: JSON.stringify({ name: props.c.name }) })
    toast(`已预设 ${props.c.name} 重建后 unhealthy，点"更新"观察自动回滚`)
    log(`[剧本] ${props.c.name} 下一次重建将模拟启动失败 → 验证回滚`, 'warn')
  } catch (e) { toast(e.message, true) }
}

// 回退：拉取版本台账 → 弹窗选择历史版本（默认选中上一个版本）
const rbOpen = ref(false)
const rbLoading = ref(false)
const rbInfo = ref(null)
const rbPick = ref('')
const rbBusy = ref(false)

const short = (d) => (d || '').replace('sha256:', '').slice(0, 12)
const sourceLabel = (s) => ({ baseline: '基线', update: '更新', rollback: '回退前', auto: '历史回填' }[s] || s)

// 实际版本：latest 等浮动 tag 解析出的真实版本号（镜像未标注版本号时回退镜像摘要，与有更新区口径一致）
const actualVersion = computed(() =>
  props.c.local_version || (props.c.local_digest ? short(props.c.local_digest) : ''))

// 回退弹窗：当前选中的版本详情行
const rbDetail = computed(() =>
  rbInfo.value?.versions?.find(v => v.digest === rbPick.value) || null)

async function openRollback() {
  menuOpen.value = false
  rbLoading.value = true
  rbOpen.value = true
  try {
    const info = await api(`/containers/${props.c.name}/versions`)
    rbInfo.value = info
    rbPick.value = info.rollback_target?.digest || ''
    if (!info.rollback_target) toast('没有可回退的历史版本（当前已是初始版本）', true)
  } catch (e) { toast(e.message, true); rbOpen.value = false }
  finally { rbLoading.value = false }
}

async function doRollback() {
  if (!rbPick.value || rbBusy.value) return
  rbBusy.value = true
  try {
    log(`手动回退 ${props.c.name} → ${short(rbPick.value)} …`)
    const r = await api(`/containers/${props.c.name}/rollback`, { method: 'POST', body: JSON.stringify({ digest: rbPick.value }) })
    toast(`${props.c.name} 已回退到 ${short(r.to_digest)}，自动更新已关闭`)
    log(`回退完成：${props.c.name} ${short(r.from_digest)} → ${short(r.to_digest)} · 自动更新已关闭`, 'ok')
    rbOpen.value = false
    emit('refresh')
  } catch (e) { toast('回退失败：' + e.message, true); log(`回退失败：${e.message}`, 'err') }
  finally { rbBusy.value = false }
}
</script>

<template>
  <div class="card-base group relative flex flex-col transition-all duration-300 hover:-translate-y-0.5 hover:shadow-lift">
    <!-- 顶部状态条（承担状态语义） -->
    <div class="h-1 rounded-t-2xl" :class="statusMeta[status].bar" />

    <div class="flex flex-col gap-3.5 p-4 flex-1">

      <!-- 头部：标题块靠左，状态徽章右锚对齐 -->
      <header class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <h3 class="text-[15px] font-bold leading-6 text-slate-900 dark:text-white truncate">{{ c.name }}</h3>
          <div class="mt-0.5 flex items-center gap-1 text-xs text-slate-400">
            <Icon name="layers" cls="w-3.5 h-3.5" />
            <span class="truncate">{{ c.compose_id || '独立容器' }}</span>
          </div>
        </div>
        <span class="badge shrink-0 !text-xs" :class="statusMeta[status].badge">{{ statusMeta[status].label }}</span>
      </header>

      <!-- 信息区：完整镜像引用 + 配置 tag（来自 docker/compose） -->
      <div class="rounded-xl bg-slate-50 dark:bg-slate-800/60 px-3 py-2 flex flex-col gap-2">
        <div class="flex items-center gap-2 h-6 text-xs" title="镜像完整引用（来自 docker inspect / compose 配置）">
          <Icon name="box" cls="w-4 h-4 text-slate-400 shrink-0" />
          <span class="font-mono text-slate-600 dark:text-slate-300 truncate">{{ c.image_spec }}</span>
        </div>

        <div class="flex items-center gap-2 h-6 text-xs">
          <Icon name="clock" cls="w-4 h-4 text-slate-400 shrink-0" />
          <span class="text-slate-400 shrink-0" title="compose / docker 配置的镜像 tag">tag</span>
          <span class="font-mono font-semibold px-2 py-0.5 rounded-md"
            :class="tagUpgrade
              ? 'bg-slate-500/10 text-slate-500 dark:text-slate-400'
              : 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'">
            {{ c.cur_tag }}
          </span>
          <!-- 该 tag 实际对应的版本号（latest 解析出的真实版本；无版本号时摘要即版本） -->
          <span v-if="actualVersion && actualVersion !== c.cur_tag"
            class="font-mono text-[11px] text-slate-500 dark:text-slate-400 truncate"
            :title="c.local_version ? '该 tag 实际对应的版本号（来自镜像标注）' : '该镜像未标注版本号，摘要即版本'">
            · {{ actualVersion }}
          </span>
          <template v-if="tagUpgrade">
            <Icon name="arrowRight" cls="w-3.5 h-3.5 text-amber-500 shrink-0" />
            <span class="font-mono font-bold px-2 py-0.5 rounded-md bg-amber-500/10 text-amber-700 dark:text-amber-400">{{ c.latest_tag }}</span>
          </template>
          <span v-else class="text-[11px] shrink-0"
            :class="c.update_available ? 'text-amber-600 dark:text-amber-400' : 'text-slate-400'">
            {{ c.update_available ? '内容有更新' : '已最新' }}
          </span>
        </div>
      </div>

      <!-- 有更新：当前版本 → 更新后版本（版本号优先，无版本标签时回退镜像摘要） -->
      <div v-if="c.update_available" class="text-[11px] leading-4 px-1">
        <div class="flex items-center gap-1.5 flex-wrap">
          <span class="text-slate-400">当前版本</span>
          <span class="font-mono font-semibold text-slate-600 dark:text-slate-300">{{ c.local_version || short(c.local_digest) || '未知' }}</span>
          <span v-if="c.local_version && c.local_digest" class="font-mono text-[10px] text-slate-400">{{ short(c.local_digest) }}</span>
          <Icon name="arrowRight" cls="w-3 h-3 text-amber-500 shrink-0" />
          <span class="text-amber-600 dark:text-amber-400">更新后</span>
          <span class="font-mono font-bold text-amber-700 dark:text-amber-400">{{ c.remote_version || short(c.remote_digest) || '未知' }}</span>
          <span v-if="c.remote_version && c.remote_digest" class="font-mono text-[10px] text-slate-400">{{ short(c.remote_digest) }}</span>
        </div>
        <p class="mt-0.5 text-slate-400">
          {{ tagUpgrade
            ? `版本号 tag ${c.cur_tag} → ${c.latest_tag}（需手动升级，自动更新仅同步当前 tag 内容）`
            : c.local_version && c.remote_version && c.local_version === c.remote_version
              ? `版本号未变（${c.cur_tag} 指向了新构建的镜像，以摘要为准），${c.update_enabled ? '开启自动时将自动重建' : '需手动执行更新'}`
              : c.local_version || c.remote_version
                ? `浮动 tag ${c.cur_tag}：远端已发布新内容，${c.update_enabled ? '开启自动时将自动重建' : '需手动执行更新'}`
                : `镜像 ${c.cur_tag} 内容已更新（该镜像未标注版本号，摘要即版本），${c.update_enabled ? '开启自动时将自动重建' : '需手动执行更新'}` }}
        </p>
      </div>

      <!-- 可选版本（弱化提示） -->
      <p v-if="hasNewer && !tagUpgrade && !c.update_available" class="text-[11px] leading-4 text-slate-400 px-1">
        仓库另有可选版本 <span class="font-mono text-slate-500 dark:text-slate-400">{{ (c.newer_tags || []).slice(0, 3).join(' / ') }}</span>（需手动升级）
      </p>

      <!-- 操作区：统一 28px 控件高度 -->
      <footer class="flex items-center gap-2">
        <div class="relative shrink-0">
          <select :value="c.mode" @change="setMode($event.target.value)"
            class="h-7 appearance-none cursor-pointer rounded-md border border-slate-200 dark:border-slate-600
                   bg-transparent pl-2.5 pr-6 text-xs font-medium text-slate-500 dark:text-slate-400
                   hover:border-brand-400 hover:text-brand-600 transition-colors focus:outline-none">
            <option v-for="m in MODES" :key="m.v" :value="m.v">{{ m.t }}</option>
          </select>
          <Icon name="arrowRight" cls="w-3 h-3 absolute right-1.5 top-1/2 -translate-y-1/2 rotate-90 pointer-events-none text-slate-400" />
        </div>

        <label class="flex items-center gap-1.5 cursor-pointer select-none shrink-0 h-7"
          title="发现新版本时自动更新（大版本升级仍需人工）">
          <span class="text-xs text-slate-500 dark:text-slate-400">自动</span>
          <span class="relative inline-block w-8 h-[18px]">
            <input type="checkbox" class="sr-only peer" :checked="!!c.update_enabled && !c.ignored" :disabled="c.ignored || c.protected"
              @change="toggleUpdate($event.target.checked)" />
            <span class="absolute inset-0 rounded-full bg-slate-300 dark:bg-slate-600 peer-checked:bg-brand-600
                         transition-colors duration-200"></span>
            <span class="absolute top-[3px] left-[3px] w-3 h-3 rounded-full bg-white shadow
                         transition-transform duration-200 peer-checked:translate-x-[14px]"></span>
          </span>
        </label>

        <span class="flex-1"></span>

        <button v-if="!c.ignored && !c.protected" @click="update()"
          class="h-7 px-3 inline-flex items-center gap-1 rounded-md bg-brand-600 hover:bg-brand-700
                 text-white text-xs font-semibold transition-colors active:scale-95">
          <Icon name="rocket" cls="w-3.5 h-3.5" /> 更新
        </button>

        <!-- 更多菜单：所有卡片统一保留（点击外部/Esc 关闭） -->
        <div ref="menuRef" class="relative shrink-0" v-if="!c.protected">
          <button @click="menuOpen = !menuOpen"
            class="h-7 w-7 inline-flex items-center justify-center rounded-md
                   border border-slate-200 dark:border-slate-600 text-slate-400
                   hover:border-brand-400 hover:text-brand-600 transition-colors"
            :class="{ 'border-brand-400 text-brand-600': menuOpen }" title="更多操作">
            <Icon name="menu" cls="w-3.5 h-3.5" />
          </button>
          <Transition name="pop">
            <div v-if="menuOpen"
              class="absolute right-0 bottom-[calc(100%+6px)] z-30 w-44 rounded-xl overflow-hidden shadow-lift
                     bg-white dark:bg-slate-800 border border-slate-200/80 dark:border-slate-700 py-1 animate-pop">
              <button v-if="demo" @click="simFail(); menuOpen = false"
                class="w-full flex items-center gap-2 px-3.5 py-2 text-xs font-medium text-amber-600 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-500/10 transition-colors">
                <Icon name="fire" cls="w-3.5 h-3.5" /> 模拟失败剧本
              </button>
              <button @click="openRollback()"
                class="w-full flex items-center gap-2 px-3.5 py-2 text-xs font-medium text-sky-600 dark:text-sky-400 hover:bg-sky-50 dark:hover:bg-sky-500/10 transition-colors">
                <Icon name="refresh" cls="w-3.5 h-3.5" /> 回退版本
              </button>
              <button v-if="c.ignored" @click="toggleIgnore(false); menuOpen = false"
                class="w-full flex items-center gap-2 px-3.5 py-2 text-xs font-medium text-emerald-600 dark:text-emerald-400 hover:bg-emerald-50 dark:hover:bg-emerald-500/10 transition-colors">
                <Icon name="check" cls="w-3.5 h-3.5" /> 恢复监控
              </button>
              <button v-else @click="toggleIgnore(true); menuOpen = false"
                class="w-full flex items-center gap-2 px-3.5 py-2 text-xs font-medium text-rose-600 dark:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-500/10 transition-colors">
                <Icon name="eye" cls="w-3.5 h-3.5" /> 忽略此容器
              </button>
            </div>
          </Transition>
        </div>
      </footer>

      <!-- 实际生效状态说明（问题 3）：模式 × 开关 → 一眼读懂 -->
      <div class="flex items-center gap-1.5 text-[11px] leading-4 px-0.5 -mt-1" :class="effectHint.cls">
        <Icon :name="effectHint.icon" cls="w-3.5 h-3.5 shrink-0" />
        <span class="truncate">{{ effectHint.text }}</span>
      </div>
    </div>

      <!-- 回退版本选择弹窗：台账全量版本任选（版本号优先，摘要回退） -->
    <Modal :open="rbOpen" :title="`回退版本 · ${c.name}`" @close="rbOpen = false">
      <div v-if="rbLoading" class="py-6 text-center text-xs text-slate-400">加载版本历史…</div>
      <template v-else-if="rbInfo">
        <p class="text-[12px] text-slate-500 dark:text-slate-400">
          当前运行
          <span class="font-mono font-semibold text-slate-700 dark:text-slate-200">{{ rbInfo.current_version || short(rbInfo.current_digest) }}</span>
          <span v-if="rbInfo.current_version" class="font-mono text-[10px] text-slate-400">（{{ short(rbInfo.current_digest) }}）</span>
        </p>
        <!-- 下拉选择历史版本（版本号 · 摘要），选中后下方展示完整信息 -->
        <div v-if="rbInfo.versions?.length" class="mt-3 relative">
          <label class="block text-[11px] text-slate-400 mb-1.5" for="rb-select">选择要回退到的版本</label>
          <select id="rb-select" v-model="rbPick"
            class="w-full h-9 appearance-none cursor-pointer rounded-lg border border-slate-200 dark:border-slate-600
                   bg-white dark:bg-slate-800 pl-3 pr-8 text-xs font-mono text-slate-700 dark:text-slate-200
                   hover:border-brand-400 transition-colors focus:outline-none focus:ring-1 focus:ring-brand-400">
            <option v-for="v in rbInfo.versions" :key="v.digest" :value="v.digest" :disabled="v.is_current">
              {{ v.version || short(v.digest) }} · {{ short(v.digest) }}{{ v.is_current ? '（当前）' : '' }}
            </option>
          </select>
          <Icon name="arrowRight" cls="w-3.5 h-3.5 absolute right-2.5 top-[30px] -translate-y-1/2 rotate-90 pointer-events-none text-slate-400" />
        </div>
        <!-- 选中版本详情 -->
        <div v-if="rbDetail" class="mt-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/60 px-3 py-2.5 space-y-1.5">
          <div class="flex items-center gap-2 text-xs">
            <span class="text-slate-400 shrink-0">版本号</span>
            <span class="font-mono font-semibold text-slate-700 dark:text-slate-200">{{ rbDetail.version || '未标注（摘要即版本）' }}</span>
          </div>
          <div class="flex items-center gap-2 text-xs">
            <span class="text-slate-400 shrink-0">镜像摘要</span>
            <span class="font-mono text-slate-600 dark:text-slate-300 truncate">{{ short(rbDetail.digest) }}</span>
            <span class="badge !text-[10px] shrink-0"
              :class="rbDetail.is_current ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' : 'bg-slate-500/10 text-slate-500 dark:text-slate-400'">
              {{ rbDetail.is_current ? '当前' : sourceLabel(rbDetail.source) }}
            </span>
          </div>
          <div class="flex items-center gap-2 text-xs">
            <span class="text-slate-400 shrink-0">记录时间</span>
            <span class="text-slate-600 dark:text-slate-300">{{ fmtTime(rbDetail.created_at) }}</span>
          </div>
        </div>
        <div v-if="!rbInfo.versions?.length" class="py-4 text-center text-xs text-slate-400">
          暂无版本历史——首次扫描建基线后，后续每次更新都会记录版本，可随时回退
        </div>
        <p class="mt-3 text-[11px] leading-4 text-amber-600 dark:text-amber-400 flex items-start gap-1.5">
          <Icon name="bell" cls="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <span>回退后将自动关闭该容器的自动更新开关；如需恢复，请重新打开「自动」。</span>
        </p>
        <div class="mt-4 flex justify-end gap-2.5">
          <button @click="rbOpen = false" class="btn-ghost !px-4">取消</button>
          <button @click="doRollback()" :disabled="!rbPick || rbBusy"
            class="btn !px-4 text-white bg-brand-600 hover:bg-brand-700 transition-colors active:scale-95 disabled:opacity-50">
            {{ rbBusy ? '回退中…' : '回退到此版本' }}
          </button>
        </div>
      </template>
    </Modal>
  </div>
</template>
