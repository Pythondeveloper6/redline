import { Navigate, Route, Routes } from 'react-router-dom'
import Shell from './components/Shell'
import Requests from './pages/Requests'
import RequestDetail from './pages/RequestDetail'
import Endpoints from './pages/Endpoints'
import Issues from './pages/Issues'
import Compare from './pages/Compare'
import History from './pages/History'
import Load from './pages/Load'

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/requests" replace />} />
        <Route path="/requests" element={<Requests />} />
        <Route path="/requests/:id" element={<RequestDetail />} />
        <Route path="/compare/:a/:b" element={<Compare />} />
        <Route path="/endpoints" element={<Endpoints />} />
        <Route path="/issues" element={<Issues />} />
        <Route path="/history" element={<History />} />
        <Route path="/load" element={<Load />} />
        <Route path="/load/:id" element={<Load />} />
        <Route path="*" element={<Navigate to="/requests" replace />} />
      </Route>
    </Routes>
  )
}
