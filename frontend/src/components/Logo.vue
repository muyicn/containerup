<script setup>
// 统一品牌 Logo：品牌渐变圆角方块 + 白色盾牌 + 蓝色对勾
// 语义：盾牌 = 守望防护，对勾 = 更新健康。与 favicon / 企微头像同源。
</script>

<template>
  <svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <defs>
      <linearGradient id="vt-logo-g" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
        <stop offset="0" stop-color="#3b6ef6" />
        <stop offset="1" stop-color="#1e36af" />
      </linearGradient>
    </defs>
    <rect width="64" height="64" rx="14" fill="url(#vt-logo-g)" />
    <path d="M32 12l16 6v12c0 10-7 18-16 22-9-4-16-12-16-22V18l16-6z" fill="#fff" />
    <path d="M25 32.5l5 5 9.5-10.5" fill="none" stroke="#1d40d8" stroke-width="4"
      stroke-linecap="round" stroke-linejoin="round" />
  </svg>
</template>
