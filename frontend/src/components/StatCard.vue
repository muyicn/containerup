<script setup>
import Icon from './Icon.vue'

defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], required: true },
  icon: { type: String, required: true },
  tone: { type: String, default: 'brand' },   // brand | green | amber | slate
  active: { type: Boolean, default: false },
})

defineEmits(['select'])

const toneMap = {
  brand: { ring: 'ring-brand-500/30', chip: 'bg-brand-500/10 text-brand-600 dark:text-brand-400', bar: 'from-brand-500 to-brand-600' },
  green: { ring: 'ring-emerald-500/30', chip: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400', bar: 'from-emerald-400 to-emerald-600' },
  amber: { ring: 'ring-amber-500/30', chip: 'bg-amber-500/10 text-amber-600 dark:text-amber-400', bar: 'from-amber-400 to-amber-500' },
  slate: { ring: 'ring-slate-400/30', chip: 'bg-slate-500/10 text-slate-500 dark:text-slate-400', bar: 'from-slate-300 to-slate-400' },
}
</script>

<template>
  <button
    @click="$emit('select')"
    class="card-base group relative overflow-hidden text-left p-5 transition-all duration-300
           hover:-translate-y-0.5 hover:shadow-lift cursor-pointer"
    :class="[active ? `ring-2 ${toneMap[tone].ring}` : '']"
  >
    <div class="absolute top-0 left-0 right-0 h-[3px] bg-gradient-to-r opacity-80" :class="toneMap[tone].bar" />
    <div class="flex items-center justify-between gap-3">
      <div class="min-w-0">
        <div class="text-[26px] font-extrabold leading-8 tracking-tight text-slate-900 dark:text-white">{{ value }}</div>
        <div class="mt-1 text-xs text-slate-400 font-medium truncate">{{ label }}</div>
      </div>
      <div class="w-9 h-9 rounded-lg flex items-center justify-center shrink-0 transition-transform duration-300 group-hover:scale-110" :class="toneMap[tone].chip">
        <Icon :name="icon" cls="w-[18px] h-[18px]" />
      </div>
    </div>
  </button>
</template>
