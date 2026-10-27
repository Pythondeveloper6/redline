import { useState } from 'react'
import { buildCurl, buildHttpie, varsNote, type Built } from '../lib/curl'
import { copyText } from '../lib/format'
import type { Trace } from '../types'
import { useFeatures } from '../lib/meta'
import ReplayModal from './ReplayModal'
import { I, Icon } from './ui'

/** Copy as cURL / HTTPie and Replay, for the request detail header. */
export default function RequestActions({ trace, offline = false }: { trace: Trace; offline?: boolean }) {
  const [replay, setReplay] = useState(false)
  const replayOn = useFeatures().has('replay')
  const canReplay = !offline && replayOn
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null)
  const spec = { method: trace.method, path: trace.path, query: trace.request.query, headers: trace.request.headers, body: trace.request.body }

  const copy = async (build: (...a: [typeof spec, string]) => Built, name: string) => {
    const { command, vars } = build(spec, location.origin)
    const ok = await copyText(command)
    const note = varsNote(vars)
    setMsg({ ok, text: ok ? `Copied ${name} command. ${note}` : `Could not copy to the clipboard.` })
    window.setTimeout(() => setMsg(null), note ? 9000 : 2500)
  }
  return (
    <>
      <button className="btn" onClick={() => copy(buildCurl, 'cURL')}><Icon d={I.copy} size={12} />Copy as cURL</button>
      <button className="btn" onClick={() => copy(buildHttpie, 'HTTPie')}><Icon d={I.copy} size={12} />Copy as HTTPie</button>
      {canReplay && <button className="btn" onClick={() => setReplay(true)}><Icon d={I.play} size={11} />Replay</button>}
      {msg && <span role="status" className={`text-[12px] ${msg.ok ? 'text-muted' : 'text-bad'}`}>{msg.text}</span>}
      {canReplay && <ReplayModal trace={trace} open={replay} onClose={() => setReplay(false)} />}
    </>
  )
}
