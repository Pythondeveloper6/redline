import { Modal } from './Modal'
import { Kbd } from './ui'

const mod = typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent) ? '⌘' : 'Ctrl'

const SECTIONS: { title: string; rows: [string[], string][] }[] = [
  { title: 'Anywhere', rows: [[[mod, 'K'], 'Open the command palette'], [['/'], 'Search (Requests: focus the filter; elsewhere: palette)'], [['?'], 'Show this help'], [['Esc'], 'Close dialogs, leave a field']] },
  { title: 'Requests list', rows: [[['j'], 'Next request'], [['k'], 'Previous request'], [['Enter'], 'Open the selected request'], [['x'], 'Select for compare (max 2)'], [['c'], 'Compare the two selected']] },
  { title: 'Command palette', rows: [[['↑', '↓'], 'Move'], [['Enter'], 'Open'], [['Esc'], 'Close']] },
]

export default function ShortcutsHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Modal open={open} onClose={onClose} title="Keyboard shortcuts" width="max-w-md">
      <div className="p-4 space-y-4 overflow-y-auto">
        {SECTIONS.map(s => (
          <section key={s.title}>
            <h3 className="m-0 mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint">{s.title}</h3>
            <dl className="m-0 space-y-1">
              {s.rows.map(([keys, label]) => (
                <div key={label} className="flex items-center gap-3">
                  <dt className="w-24 shrink-0 flex gap-1">{keys.map(k => <Kbd key={k}>{k}</Kbd>)}</dt>
                  <dd className="m-0 text-muted">{label}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </Modal>
  )
}
