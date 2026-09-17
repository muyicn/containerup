/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js}'],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['-apple-system', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Segoe UI', 'Noto Sans SC', 'sans-serif'],
        mono: ['Cascadia Code', 'JetBrains Mono', 'SF Mono', 'Consolas', 'monospace'],
      },
      colors: {
        brand: {
          50: '#eef4ff', 100: '#dbe6fe', 200: '#bfd3fe', 300: '#93b4fd',
          400: '#6090fa', 500: '#3b6ef6', 600: '#2553eb', 700: '#1d40d8',
          800: '#1e36af', 900: '#1e328a', 950: '#172054',
        },
      },
      boxShadow: {
        soft: '0 2px 12px rgba(15,23,42,.06)',
        lift: '0 8px 30px rgba(15,23,42,.12)',
        glow: '0 0 24px rgba(59,110,246,.25)',
      },
      animation: {
        'fade-in': 'fadeIn .25s ease both',
        'slide-up': 'slideUp .35s cubic-bezier(.16,1,.3,1) both',
        'slide-in-right': 'slideInRight .3s cubic-bezier(.16,1,.3,1) both',
        'pop': 'pop .25s cubic-bezier(.34,1.56,.64,1) both',
      },
      keyframes: {
        fadeIn: { from: { opacity: 0 }, to: { opacity: 1 } },
        slideUp: { from: { opacity: 0, transform: 'translateY(14px)' }, to: { opacity: 1, transform: 'none' } },
        slideInRight: { from: { opacity: 0, transform: 'translateX(28px)' }, to: { opacity: 1, transform: 'none' } },
        pop: { from: { opacity: 0, transform: 'scale(.92)' }, to: { opacity: 1, transform: 'scale(1)' } },
      },
    },
  },
  plugins: [],
}
