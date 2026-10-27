import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useFocusTrap } from '../lib/focus'
import { I, Icon } from './ui'

/** Accessible centered dialog: portal, Escape to close, focus trap, focus restore. */
export function Modal({ open, onClose, title, children, width = 'max-w-lg', top = false, label }: {
  open: boolean; onClose: () => void; title?: ReactNode; children: ReactNode; width?: string; top?: boolean; label?: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  useFocusTrap(ref, open)
  useEffect(() => {
    if (!open) return
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); onClose() } }
    window.addEventListener('keydown', h, true)
    return () => window.removeEventListener('keydown', h, true)
  }, [open, onClose])
  if (!open) return null
  return createPortal(
    <div className={`fixed inset-0 z-50 flex justify-center p-3 sm:p-4 ${top ? 'items-start pt-[10vh]' : 'items-center'}`}>
      <div className="absolute inset-0 bg-black/50 anim-fade" onClick={onClose} />
      <div ref={ref} role="dialog" aria-modal="true" aria-label={label ?? (typeof title === 'string' ? title : undefined)} tabIndex={-1}
        className={`relative card w-full ${width} max-h-[85vh] flex flex-col shadow-2xl anim-fade outline-none overflow-hidden`}>
        {title != null && (
          <header className="flex items-center gap-3 px-4 py-3 border-b border-line">
            <div className="flex-1 min-w-0 text-[14px] font-semibold truncate">{title}</div>
            <button className="btn btn-ghost !px-1.5" onClick={onClose} aria-label="Close"><Icon d={I.x} size={16} /></button>
          </header>
        )}
        {children}
      </div>
    </div>,
    document.body,
  )
}
