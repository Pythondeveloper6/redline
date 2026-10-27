import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Callsite, FeatureName, Meta } from '../types'

let cached: Promise<Meta | null> | null = null
/** Meta is fetched once per page load and shared; failures resolve to null (older backend / no access). */
export function loadMeta(): Promise<Meta | null> {
  if (!cached) cached = api.meta().catch(() => null)
  return cached
}
export function useMeta(): Meta | null {
  const [m, setM] = useState<Meta | null>(null)
  useEffect(() => { let live = true; loadMeta().then(v => live && setM(v)); return () => { live = false } }, [])
  return m
}

const isAbs = (p: string) => p.startsWith('/') || /^[A-Za-z]:[\\/]/.test(p)

/** Absolute path of a callsite file, or null when it cannot be determined. */
export function absolutePath(root: string | null | undefined, file: string): string | null {
  if (isAbs(file)) return file
  if (!root) return null
  const sep = root.includes('\\') && !root.includes('/') ? '\\' : '/'
  return root.replace(/[\\/]+$/, '') + sep + file.replace(/^\.?[\\/]+/, '')
}

/** Builds the open-in-editor URL from `meta.editor_url_template`; null when not configured. */
export function editorUrl(meta: Pick<Meta, 'editor_url_template' | 'project_root'> | null | undefined, cs: Pick<Callsite, 'file' | 'line'>): string | null {
  const tpl = meta?.editor_url_template
  if (!tpl) return null
  const abs = absolutePath(meta?.project_root, cs.file)
  if (!abs) return null
  const file = encodeURI(abs).replace(/#/g, '%23').replace(/\?/g, '%3F')
  return tpl.replace(/\{file\}/g, () => file).replace(/\{line\}/g, () => String(cs.line))
}

/** Feature switch from `meta.features`. A missing key means enabled (older backends), except `load_from_ui` and `checks_from_ui`. */
export function feature(meta: Pick<Meta, 'features'> | null | undefined, name: FeatureName): boolean {
  const v = meta?.features?.[name]
  return v === undefined ? name !== 'load_from_ui' && name !== 'checks_from_ui' : !!v
}
/** True when the instance is read-only (`meta.access.read_only`): hide every mutating action. */
export function isReadOnly(meta: Pick<Meta, 'access'> | null | undefined): boolean {
  return !!meta?.access?.read_only
}
/** Shared meta plus the feature helpers. Before meta has loaded, features read as enabled and not read-only. */
/** POSTs ./api/logout (clears the session cookie) and reloads. */
export async function logout() {
  try { await api.logout() } catch { /* already signed out */ }
  location.reload()
}
export function useFeatures() {
  const meta = useMeta()
  return { meta, has: (n: FeatureName) => feature(meta, n), readOnly: isReadOnly(meta), loaded: meta !== null }
}
