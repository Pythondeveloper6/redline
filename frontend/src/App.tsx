import { useSyncExternalStore } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { getLoginRequired, subscribeLoginRequired } from './api'
import Shell from './components/Shell'
import Requests from './pages/Requests'
import RequestDetail from './pages/RequestDetail'
import Endpoints from './pages/Endpoints'
import Issues from './pages/Issues'
import Compare from './pages/Compare'
import History from './pages/History'
import Load from './pages/Load'
import Checks from './pages/Checks'
import CheckBuilder from './pages/CheckBuilder'
import CheckSuite from './pages/CheckSuite'
import CheckRun from './pages/CheckRun'
import Database from './pages/Database'
import Cache from './pages/Cache'
import External from './pages/External'
import Settings from './pages/Settings'
import Analysis from './pages/Analysis'
import Jobs from './pages/Jobs'
import Login from './pages/Login'
import SnapshotApp from './SnapshotApp'
import { getSnapshot } from './lib/snapshot'

export default function App() {
  const login = useSyncExternalStore(subscribeLoginRequired, getLoginRequired)
  if (getSnapshot()) return <SnapshotApp />
  if (login) return <Login kind={login} />
  return (
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
  )
}
