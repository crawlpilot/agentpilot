import { createBrowserRouter } from 'react-router-dom'
import App from './App'
import { LoginPage } from '@/routes/LoginPage'
import { DashboardPage } from '@/routes/DashboardPage'
import { SessionsListPage } from '@/routes/SessionsListPage'
import { SessionDetailPage } from '@/routes/SessionDetailPage'
import { PlaygroundPage } from '@/routes/PlaygroundPage'
import { ApiKeysPage } from '@/routes/ApiKeysPage'

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'sessions', element: <SessionsListPage /> },
      { path: 'sessions/:sessionId', element: <SessionDetailPage /> },
      { path: 'playground', element: <PlaygroundPage /> },
      { path: 'api-keys', element: <ApiKeysPage /> },
    ],
  },
])
