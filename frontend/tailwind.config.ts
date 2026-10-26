import type { Config } from 'tailwindcss'

const c = (n: string) => `rgb(var(--${n}) / <alpha-value>)`
export default {
  darkMode: ['selector', '[data-theme="dark"]'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: c('bg'), surface: c('surface'), raised: c('raised'), line: c('line'),
        ink: c('ink'), muted: c('muted'), faint: c('faint'), accent: c('accent'),
        ok: c('ok'), warn: c('warn'), bad: c('bad'), info: c('info'),
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
} satisfies Config
