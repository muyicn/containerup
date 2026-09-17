<script setup>
import { onMounted, reactive, ref } from 'vue'
import { store, toast, log, confirmDialog } from '../store'
import { api } from '../api'
import Icon from '../components/Icon.vue'

const channels = ref([])
const settings = reactive({ delay_update_sec: '0', scan_interval_sec: '0', public_base_url: '' })
const chForm = reactive({ kind: 'webhook', name: '', url: '' })
const pwdForm = reactive({ old_password: '', new_password: '', confirm: '' })
const pwdBusy = ref(false)

async function load() {
  channels.value = await api('/channels')
  const s = await api('/settings')
  settings.delay_update_sec = s.delay_update_sec || '0'
  settings.scan_interval_sec = s.scan_interval_sec || '0'
  settings.public_base_url = s.public_base_url || ''
}

async function addChannel() {
  if (!chForm.url.trim()) return toast('请填写渠道地址', true)
  try {
    await api('/channels', { method: 'POST', body: JSON.stringify({ kind: chForm.kind, name: chForm.name || '渠道', url: chForm.url.trim() }) })
    toast('渠道已添加')
    log(`添加通知渠道：${chForm.name || chForm.kind}`)
    Object.assign(chForm, { kind: 'webhook', name: '', url: '' })
    await load()
  } catch (e) { toast(e.message, true) }
}

async function delChannel(id, name) {
  const ok = await confirmDialog({
    title: `删除渠道 ${name}？`,
    message: '删除后该渠道不再收到推送通知。',
    danger: true,
    okText: '删除',
  })
  if (!ok) return
  try { await api(`/channels/${id}`, { method: 'DELETE' }); await load() } catch (e) { toast(e.message, true) }
}

async function testChannel(url, kind) {
  try {
    const r = await api('/channels/test', { method: 'POST', body: JSON.stringify({ url, kind }) })
    r.delivered ? toast('✅ 渠道连通') : toast('❌ 投递失败', true)
    log(`渠道测试（${kind}）：${r.delivered ? '成功' : '失败'}`, r.delivered ? 'ok' : 'err')
  } catch (e) { toast(e.message, true) }
}

async function saveSettings() {
  try {
    await api('/settings', { method: 'PUT', body: JSON.stringify({ settings: {
      delay_update_sec: String(parseInt(settings.delay_update_sec) || 0),
      scan_interval_sec: String(parseInt(settings.scan_interval_sec) || 0),
      public_base_url: settings.public_base_url.trim(),
    } }) })
    toast('已保存，立即生效')
    log(`策略已保存：检测间隔 ${settings.scan_interval_sec}s · 发布延迟 ${settings.delay_update_sec}s · Logo 地址 ${settings.public_base_url.trim() || '未配置'}`)
  } catch (e) { toast(e.message, true) }
}

async function changePassword() {
  if (pwdForm.new_password !== pwdForm.confirm) return toast('两次输入的新密码不一致', true)
  if (pwdForm.new_password.length < 6) return toast('新密码至少 6 位', true)
  pwdBusy.value = true
  try {
    await api('/auth/password', {
      method: 'PUT',
      body: JSON.stringify({ old_password: pwdForm.old_password, new_password: pwdForm.new_password }),
    })
    toast('密码已修改，下次登录请使用新密码')
    log('管理员密码已修改', 'warn')
    Object.assign(pwdForm, { old_password: '', new_password: '', confirm: '' })
  } catch (e) { toast(e.message, true) }
  finally { pwdBusy.value = false }
}

onMounted(load)
</script>

<template>
  <div class="animate-fade-in space-y-5">
    <div>
      <h1 class="text-xl font-bold text-slate-900 dark:text-white">设置</h1>
      <p class="text-xs text-slate-400 mt-1">渠道、策略与账号管理</p>
    </div>

    <!-- 通知渠道 -->
    <div class="card-base p-5">
      <h3 class="flex items-center gap-2 text-sm font-bold text-slate-700 dark:text-slate-200 mb-4">
        <Icon name="bell" cls="w-4 h-4 text-brand-500" /> 通知渠道
      </h3>
      <div class="flex gap-2 flex-wrap">
        <select v-model="chForm.kind" class="input !w-auto">
          <option value="webhook">Webhook（JSON）</option>
          <option value="dingtalk">钉钉机器人</option>
          <option value="wecom">企业微信机器人</option>
          <option value="feishu">飞书机器人</option>
        </select>
        <input v-model="chForm.name" class="input !w-32" placeholder="名称" />
        <input v-model="chForm.url" class="input flex-1 min-w-[200px] font-mono !text-xs" placeholder="https://…（Webhook / 机器人地址）" />
        <button @click="addChannel()" class="btn-primary shrink-0">添加</button>
      </div>
      <div v-if="channels.length" class="mt-4 space-y-2">
        <div v-for="c in channels" :key="c.id" class="flex items-center gap-3 px-3.5 py-3 rounded-xl bg-slate-50 dark:bg-slate-800/60">
          <span class="badge bg-brand-500/10 text-brand-600 dark:text-brand-400 shrink-0">{{ c.kind }}</span>
          <span class="text-[13px] font-medium text-slate-700 dark:text-slate-200 shrink-0">{{ c.name }}</span>
          <span class="text-[11px] font-mono text-slate-400 truncate flex-1">{{ c.url }}</span>
          <button @click="testChannel(c.url, c.kind)" class="btn-ghost !px-2.5 !py-1 !text-[11px] shrink-0">测试</button>
          <button @click="delChannel(c.id, c.name)" class="btn-danger !px-2.5 !py-1 !text-[11px] shrink-0">删除</button>
        </div>
      </div>
      <div class="mt-4 pt-4 border-t border-slate-200/70 dark:border-slate-700/60">
        <div class="flex items-center gap-3 flex-wrap">
          <label class="text-[13px] text-slate-500 dark:text-slate-400 shrink-0">企微通知 Logo</label>
          <input v-model="settings.public_base_url" class="input flex-1 min-w-[240px] font-mono !text-xs" placeholder="http://192.168.1.10:9412（企微客户端可访问的本应用地址）" />
          <button @click="saveSettings()" class="btn-primary shrink-0">保存</button>
        </div>
        <p class="mt-1.5 text-[11px] text-slate-400 leading-5">
          填写后企微通知改用图文样式并内嵌应用 Logo（需企微客户端 4.1.36+）；留空保持默认样式。<br />
          电脑端企微填 127.0.0.1:9412 即可；手机端需填本机局域网 IP 或域名，并确保可访问本应用。
        </p>
      </div>
    </div>

    <!-- 更新策略 -->
    <div class="card-base p-5">
      <h3 class="flex items-center gap-2 text-sm font-bold text-slate-700 dark:text-slate-200 mb-4">
        <Icon name="gear" cls="w-4 h-4 text-brand-500" /> 更新策略
      </h3>
      <div class="space-y-3">
        <div class="flex items-center gap-3 flex-wrap">
          <label class="text-[13px] text-slate-500 dark:text-slate-400 w-36 shrink-0">自动检测间隔（秒）</label>
          <input v-model="settings.scan_interval_sec" type="number" min="0" class="input !w-28" />
        </div>
        <div class="flex items-center gap-3 flex-wrap">
          <label class="text-[13px] text-slate-500 dark:text-slate-400 w-36 shrink-0">全局发布延迟（秒）</label>
          <input v-model="settings.delay_update_sec" type="number" min="0" class="input !w-28" />
        </div>
        <button @click="saveSettings()" class="btn-primary">保存</button>
      </div>
      <p class="mt-2.5 text-[11.5px] text-slate-400 leading-5">
        检测间隔：每 N 秒自动扫描并在候选就绪时执行自动更新（0 = 仅手动）；保存后立即生效，无需重启。<br />
        发布延迟：新镜像发布后等待 N 秒才进入自动更新候选（0 = 禁用）。
      </p>
    </div>

    <!-- 修改密码 -->
    <div class="card-base p-5">
      <h3 class="flex items-center gap-2 text-sm font-bold text-slate-700 dark:text-slate-200 mb-4">
        <Icon name="key" cls="w-4 h-4 text-brand-500" /> 修改密码
      </h3>
      <form @submit.prevent="changePassword" class="space-y-3 max-w-sm">
        <input v-model="pwdForm.old_password" type="password" class="input" placeholder="当前密码" autocomplete="current-password" />
        <input v-model="pwdForm.new_password" type="password" class="input" placeholder="新密码（至少 6 位）" autocomplete="new-password" />
        <input v-model="pwdForm.confirm" type="password" class="input" placeholder="确认新密码" autocomplete="new-password" />
        <button type="submit" class="btn-primary w-full" :disabled="pwdBusy || !pwdForm.old_password || pwdForm.new_password.length < 6">
          <Icon name="lock" cls="w-4 h-4" /> 确认修改
        </button>
      </form>
    </div>

    <!-- 环境信息 -->
    <div class="card-base p-5">
      <h3 class="flex items-center gap-2 text-sm font-bold text-slate-700 dark:text-slate-200 mb-4">
        <Icon name="terminal" cls="w-4 h-4 text-brand-500" /> 运行环境
      </h3>
      <div class="grid grid-cols-2 gap-2 text-xs">
        <div class="px-3.5 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/60 flex justify-between">
          <span class="text-slate-500">模式</span>
          <span class="font-semibold" :class="store.demoMode ? 'text-amber-500' : 'text-emerald-500'">{{ store.demoMode ? 'DEMO' : '正常' }}</span>
        </div>
        <div class="px-3.5 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/60 flex justify-between">
          <span class="text-slate-500">Docker 引擎</span>
          <span class="font-semibold" :class="store.mockFallback ? 'text-amber-500' : 'text-emerald-500'">{{ store.mockFallback ? 'Mock（降级）' : '真实 CLI' }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
