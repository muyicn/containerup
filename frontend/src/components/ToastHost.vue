<script setup>
import { store } from '../store'
import Icon from './Icon.vue'
</script>

<template>
  <Teleport to="body">
    <div class="fixed top-4 right-4 z-[100] flex flex-col gap-2 pointer-events-none">
      <TransitionGroup name="toast">
        <div v-for="t in store.toasts" :key="t.id"
          class="pointer-events-auto flex items-start gap-2.5 max-w-sm px-4 py-3 rounded-xl shadow-lift
                 bg-white/95 dark:bg-slate-800/95 backdrop-blur-xl border animate-slide-in-right"
          :class="t.isErr ? 'border-rose-300/60 dark:border-rose-500/40' : 'border-slate-200/70 dark:border-slate-700'">
          <span class="w-6 h-6 rounded-lg flex items-center justify-center shrink-0"
            :class="t.isErr ? 'bg-rose-500/10 text-rose-500' : 'bg-emerald-500/10 text-emerald-500'">
            <Icon :name="t.isErr ? 'x' : 'check'" cls="w-3.5 h-3.5" />
          </span>
          <span class="text-[13px] leading-5 text-slate-700 dark:text-slate-200">{{ t.msg }}</span>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toast-enter-active, .toast-leave-active { transition: all .3s cubic-bezier(.16,1,.3,1); }
.toast-enter-from { opacity: 0; transform: translateX(32px); }
.toast-leave-to { opacity: 0; transform: translateX(32px) scale(.95); }
</style>
