import type { SnapshotData } from '../types'

declare global {
  interface Window { __REDLINE_SNAPSHOT__?: SnapshotData }
}

/** The inlined `{trace, profile}` of an offline snapshot page, or null in the normal app. */
export function getSnapshot(): SnapshotData | null {
  try {
    const s = window.__REDLINE_SNAPSHOT__
    return s && s.trace ? s : null
  } catch { return null }
}
export const isSnapshot = () => getSnapshot() !== null
