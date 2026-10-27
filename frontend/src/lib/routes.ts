// Page registry used by the command palette. A page (including pages added later in v0.2:
// Database, Cache, External, History, Load, ...) makes itself discoverable with one call:
//
//   registerRoute({ path: '/database', label: 'Database', keywords: 'sql index advisor' })
//
// Call it at module scope of the page (or from App.tsx). Entries are de-duplicated by path.

export interface RouteEntry { path: string; label: string; keywords?: string; hint?: string }

const routes = new Map<string, RouteEntry>()
export function registerRoute(r: RouteEntry) { routes.set(r.path, r) }
export function getRoutes(): RouteEntry[] { return [...routes.values()] }

registerRoute({ path: '/requests', label: 'Requests', keywords: 'traces list live', hint: 'Recorded requests' })
registerRoute({ path: '/endpoints', label: 'Endpoints', keywords: 'routes p95 latency stats', hint: 'Per-route latency and query stats' })
registerRoute({ path: '/issues', label: 'Issues', keywords: 'n+1 duplicate slow queries problems', hint: 'Detected problems' })
registerRoute({ path: '/history', label: 'History', keywords: 'timeline releases regressions compare periods trends', hint: 'Latency over time, release comparison' })
registerRoute({ path: '/load', label: 'Load tests', keywords: 'load runs rps knee bottlenecks stress', hint: 'Load runs and bottlenecks' })
