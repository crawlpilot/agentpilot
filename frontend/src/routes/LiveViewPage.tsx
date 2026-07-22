import { Navigate, useParams } from 'react-router-dom'
import { useAuth } from '@/lib/auth/AuthContext'
import { LiveViewPanel } from '@/components/app/LiveViewPanel'

export function LiveViewPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const { isAuthed } = useAuth()

  if (!isAuthed) return <Navigate to="/login" replace />
  if (!sessionId) return null

  return (
    <div className="h-svh w-full bg-background">
      <LiveViewPanel sessionId={sessionId} />
    </div>
  )
}
