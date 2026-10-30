import { lazy, Suspense, useSyncExternalStore } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { getLoginRequired, subscribeLoginRequired } from './api'
import Shell from './components/Shell'
import { getSnapshot } from './lib/snapshot'
import { registerRoute } from './lib/routes'

// Keep palette destinations available before their lazy page modules are visited.
[
  { path: '/database', label: 'Database', keywords: 'sql queries index advisor unused indexes fingerprints', hint: 'SQL search and index advice' },
  { path: '/cache', label: 'Cache', keywords: 'hit rate misses stampede unread sets redis memcached', hint: 'Cache hit rates, unread sets, stampedes' },
  { path: '/external', label: 'External calls', keywords: 'http outbound requests api hosts p95 retries duplicates errors', hint: 'Outbound HTTP latency, errors, retries' },
  { path: '/jobs', label: 'Jobs', keywords: 'celery task worker import retry', hint: 'Recorded background jobs' },
  { path: '/analysis', label: 'Analysis tools', keywords: 'static analyze health coverage suppressions pytest', hint: 'Source analysis and recording quality' },
  { path: '/settings', label: 'Settings', keywords: 'config configuration preset access token features sampling toml env', hint: 'Effective configuration' },
  { path: '/checks', label: 'Checks', keywords: 'refactor check baseline compare regression suite verdict', hint: 'Refactor safety checks' },
  { path: '/checks/new', label: 'New check suite', keywords: 'refactor check builder create suite endpoints', hint: 'Build a refactor check suite' },
].forEach(registerRoute)

const Requests = lazy(() => import('./pages/Requests'))
const RequestDetail = lazy(() => import('./pages/RequestDetail'))
const Endpoints = lazy(() => import('./pages/Endpoints'))
const Issues = lazy(() => import('./pages/Issues'))
const Compare = lazy(() => import('./pages/Compare'))
const History = lazy(() => import('./pages/History'))
const Load = lazy(() => import('./pages/Load'))
const Checks = lazy(() => import('./pages/Checks'))
const CheckBuilder = lazy(() => import('./pages/CheckBuilder'))
const CheckSuite = lazy(() => import('./pages/CheckSuite'))
const CheckRun = lazy(() => import('./pages/CheckRun'))
const Database = lazy(() => import('./pages/Database'))
const Cache = lazy(() => import('./pages/Cache'))
const External = lazy(() => import('./pages/External'))
const Settings = lazy(() => import('./pages/Settings'))
const Analysis = lazy(() => import('./pages/Analysis'))
const Jobs = lazy(() => import('./pages/Jobs'))
const Login = lazy(() => import('./pages/Login'))
const SnapshotApp = lazy(() => import('./SnapshotApp'))

export default function App() {
  const login = useSyncExternalStore(subscribeLoginRequired, getLoginRequired)
  return (
    <Suspense fallback={null}>
      {getSnapshot() ? <SnapshotApp /> : login ? <Login kind={login} /> : (
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<Navigate to="/requests" replace />} />
            <Route path="/requests" element={<Requests />} />
            <Route path="/requests/:id" element={<RequestDetail />} />
            <Route path="/compare/:a/:b" element={<Compare />} />
            <Route path="/endpoints" element={<Endpoints />} />
            <Route path="/issues" element={<Issues />} />
            <Route path="/database" element={<Database />} />
            <Route path="/cache" element={<Cache />} />
            <Route path="/external" element={<External />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/analysis" element={<Analysis />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/history" element={<History />} />
            <Route path="/load" element={<Load />} />
            <Route path="/load/:id" element={<Load />} />
            <Route path="/checks" element={<Checks />} />
            <Route path="/checks/new" element={<CheckBuilder />} />
            <Route path="/checks/suites/:id" element={<CheckSuite />} />
            <Route path="/checks/suites/:id/edit" element={<CheckBuilder />} />
            <Route path="/checks/runs/:id" element={<CheckRun />} />
            <Route path="*" element={<Navigate to="/requests" replace />} />
          </Route>
        </Routes>
      )}
    </Suspense>
  )
}
