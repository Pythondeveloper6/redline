import { useState } from 'react'
import { api, ApiError, type LoginKind } from '../api'
import { Logo } from '../components/Shell'
import { I, Icon } from '../components/ui'

/** Full-page sign-in, shown when the API answers 401 `login_required`. The session lives in an HttpOnly cookie; nothing is stored in the browser. */
export default function Login({ kind }: { kind: LoginKind }) {
  const [token, setToken] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const basic = kind === 'basic'
  const ready = basic ? username.trim() !== '' && password !== '' : token !== ''

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy || !ready) return
    setBusy(true); setErr(null)
    try {
      await api.login(basic ? { username, password } : { token })
      location.reload() // resets the cached meta and every page's data
    } catch (x) {
      const a = x as ApiError
      setErr(a.code === 'too_many_requests' && a.retryAfter
        ? `${a.message} Try again in ${a.retryAfter} ${a.retryAfter === 1 ? 'second' : 'seconds'}.`
        : a.message || 'Sign-in failed.')
      setBusy(false)
    }
  }

  const field = 'h-9 w-full rounded-md border border-line bg-surface px-2.5 text-[13px] outline-none focus:border-accent'
  return (
    <div className="min-h-full grid place-items-center px-4 py-10">
      <form onSubmit={submit} className="card w-full max-w-sm p-5 space-y-4" aria-labelledby="login-title">
        <div className="flex flex-col items-center gap-2 text-center">
          <Logo />
          <h1 id="login-title" className="m-0 text-[15px] font-semibold">Sign in to continue</h1>
          <p className="m-0 text-[12px] text-muted">{basic ? 'This Redline instance is protected. Enter your username and password.' : 'This Redline instance is protected. Enter the access token to view recorded requests.'}</p>
        </div>
        {basic ? (
          <>
            <label className="block space-y-1">
              <span className="text-[12px] font-medium">Username</span>
              <input className={field} type="text" name="username" autoComplete="username" autoFocus value={username} onChange={e => setUsername(e.target.value)} />
            </label>
            <label className="block space-y-1">
              <span className="text-[12px] font-medium">Password</span>
              <input className={field} type="password" name="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} />
            </label>
          </>
        ) : (
          <label className="block space-y-1">
            <span className="text-[12px] font-medium">Access token</span>
            <input className={`${field} mono`} type="password" name="token" autoComplete="current-password" autoFocus spellCheck={false} value={token} onChange={e => setToken(e.target.value)} />
          </label>
        )}
        {err && <div role="alert" className="rounded-md border border-bad/30 bg-bad/5 p-2 text-bad text-[12px] flex items-start gap-2"><Icon d={I.alert} size={14} className="mt-px shrink-0" /><span className="break-words">{err}</span></div>}
        <button type="submit" className="btn btn-accent w-full justify-center !h-9" disabled={busy || !ready}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
    </div>
  )
}
