import { useEffect, type RefObject } from 'react'

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'

/** Keeps Tab / Shift-Tab inside `ref` while `active`, and restores focus to the previously focused element on close. */
export function useFocusTrap(ref: RefObject<HTMLElement | null>, active: boolean) {
  useEffect(() => {
    if (!active) return
    const prev = document.activeElement as HTMLElement | null
    const el = ref.current
    if (el && !el.contains(document.activeElement)) {
      (el.querySelector<HTMLElement>('[data-autofocus]') ?? el.querySelector<HTMLElement>(FOCUSABLE) ?? el).focus()
    }
    const h = (e: KeyboardEvent) => {
      if (e.key !== 'Tab' || !ref.current) return
      const items = [...ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(n => n.offsetParent !== null || n === document.activeElement)
      if (items.length === 0) { e.preventDefault(); ref.current.focus(); return }
      const first = items[0]; const last = items[items.length - 1]
      const cur = document.activeElement
      if (e.shiftKey && (cur === first || !ref.current.contains(cur))) { e.preventDefault(); last.focus() }
      else if (!e.shiftKey && (cur === last || !ref.current.contains(cur))) { e.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', h)
    return () => { document.removeEventListener('keydown', h); if (prev && document.contains(prev)) prev.focus() }
  }, [active, ref])
}
