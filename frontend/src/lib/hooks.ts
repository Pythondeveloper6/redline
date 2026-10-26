import { useEffect, useState } from 'react'

export function useNow(intervalMs = 5000) {
  const [now, setNow] = useState(Date.now() / 1000)
  useEffect(() => { const t = setInterval(() => setNow(Date.now() / 1000), intervalMs); return () => clearInterval(t) }, [intervalMs])
  return now
}
export function useDebounced<T>(v: T, delay = 250) {
  const [d, setD] = useState(v)
  useEffect(() => { const t = setTimeout(() => setD(v), delay); return () => clearTimeout(t) }, [v, delay])
  return d
}
export function useMedia(q: string) {
  const [m, setM] = useState(() => matchMedia(q).matches)
  useEffect(() => { const mq = matchMedia(q); const h = () => setM(mq.matches); mq.addEventListener('change', h); return () => mq.removeEventListener('change', h) }, [q])
  return m
}
/** True when the keyboard event target is a text field. */
export const isTyping = (e: KeyboardEvent) => {
  const t = e.target as HTMLElement | null
  return !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)
}
