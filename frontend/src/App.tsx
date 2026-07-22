import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { NavSidebar } from '@/components/app/NavSidebar'
import { TopBar } from '@/components/app/TopBar'
import { useAuth } from '@/lib/auth/AuthContext'

function App() {
  const { isAuthed } = useAuth()
  const location = useLocation()

  if (!isAuthed) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }

  return (
    <div className="flex h-svh w-full">
      <NavSidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main className="flex-1 overflow-y-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

export default App
