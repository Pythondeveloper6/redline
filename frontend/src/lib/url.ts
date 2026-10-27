// Same-origin guards for recorded request paths. A recorded path comes from the request line of
// whatever client hit the app, so it is untrusted: `//evil.example/x`, `/\evil.example` or a path
// with a tab/newline (stripped by the URL parser, turning `/\t/evil` into `//evil`) would resolve
// to another origin and take the captured headers and re-typed secrets with them.

const CONTROL = /[\u0000-\u001f\u007f]/

/** The recorded path resolved against `origin`, or `null` unless it is a plain same-origin path. */
export function sameOriginUrl(path: string, origin: string): URL | null {
  if (typeof path !== 'string' || !/^\/(?![/\\])/.test(path) || CONTROL.test(path)) return null
  let base: URL
  let url: URL
  try {
    base = new URL(origin)
    url = new URL(path, base.origin)
  } catch {
    return null
  }
  return url.origin === base.origin ? url : null
}

/**
 * `path` made safe to append to an origin: control characters dropped and any run of leading
 * slashes/backslashes collapsed to one `/`, so the result always stays on that origin.
 */
export function safePath(path: string): string {
  const clean = String(path).replace(new RegExp(CONTROL.source, 'g'), '')
  return '/' + clean.replace(/^[/\\]+/, '')
}
