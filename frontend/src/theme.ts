import { useSyncExternalStore } from 'react'

export type ThemePref = 'system' | 'light' | 'dark'
const KEY = 'redline-theme'
const listeners = new Set<() => void>()

function read(): ThemePref {
  try { const v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : 'system' } catch { return 'system' }
}
function apply(p: ThemePref) {
  const dark = p === 'dark' || (p === 'system' && matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light')
}
let pref = read()
apply(pref)
try {
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (pref === 'system') { apply(pref); listeners.forEach(l => l()) } })
} catch { /* ignore */ }

export function setTheme(p: ThemePref) {
  pref = p
  try { if (p === 'system') localStorage.removeItem(KEY); else localStorage.setItem(KEY, p) } catch { /* ignore */ }
  apply(p)
  listeners.forEach(l => l())
}
export function useTheme(): ThemePref {
  return useSyncExternalStore(cb => { listeners.add(cb); return () => listeners.delete(cb) }, () => pref)
}
export const useIsDark = () => { useTheme(); return document.documentElement.getAttribute('data-theme') === 'dark' }
