<script setup>
import Icon from './Icon.vue'

defineProps({
  open: { type: Boolean, required: true },
  title: { type: String, default: '' },
  tone: { type: String, default: 'brand' },  // brand | danger
})
defineEmits(['close'])
</script>

<template>
  <Teleport to="body">
    <Transition name="mask">
      <div v-if="open" class="fixed inset-0 z-[90] bg-slate-900/45 backdrop-blur-sm flex items-center justify-center p-4" @click.self="$emit('close')">
        <Transition name="pop" appear>
          <div class="w-full max-w-sm card-base !rounded-2xl shadow-lift overflow-hidden animate-pop">
            <div class="h-1 bg-gradient-to-r" :class="tone === 'danger' ? 'from-rose-400 to-rose-600' : 'from-brand-400 to-brand-600'" />
            <div class="px-5 pt-4 pb-2">
              <div class="flex items-center gap-2">
                <Icon v-if="tone === 'danger'" name="fire" cls="w-5 h-5 text-rose-500" />
                <Icon v-else name="bolt" cls="w-5 h-5 text-brand-500" />
                <h3 class="font-bold text-[15px] text-slate-900 dark:text-white">{{ title }}</h3>
                <button @click="$emit('close')" class="ml-auto p-1 rounded-lg text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors">
                  <Icon name="x" cls="w-4 h-4" />
                </button>
              </div>
            </div>
            <div class="px-5 pb-5">
              <slot />
            </div>
          </div>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.mask-enter-active, .mask-leave-active { transition: opacity .2s ease; }
.mask-enter-from, .mask-leave-to { opacity: 0; }
.pop-enter-active, .pop-leave-active { transition: all .25s cubic-bezier(.34,1.56,.64,1); }
.pop-enter-from, .pop-leave-to { opacity: 0; transform: scale(.9); }
</style>
