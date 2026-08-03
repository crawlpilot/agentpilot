import { createBrowserRouter, Navigate } from 'react-router-dom'
import App from './App'
import { LoginPage } from '@/routes/LoginPage'
import { DashboardPage } from '@/routes/DashboardPage'
import { SessionsListPage } from '@/routes/SessionsListPage'
import { SessionDetailPage } from '@/routes/SessionDetailPage'
import { LiveViewPage } from '@/routes/LiveViewPage'
import { PlaygroundPage } from '@/routes/PlaygroundPage'
import { PlaygroundScrapeTab } from '@/routes/PlaygroundScrapeTab'
import { PlaygroundMapTab } from '@/routes/PlaygroundMapTab'
import { PlaygroundCrawlTab } from '@/routes/PlaygroundCrawlTab'
import { PlaygroundInteractTab } from '@/routes/PlaygroundInteractTab'
import { ApiKeysPage } from '@/routes/ApiKeysPage'
import { NodesPage } from '@/routes/NodesPage'

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  // Standalone, no sidebar/topbar shell -- meant to be opened in its own
  // tab (see SessionsTable's "Live" link) as a focused viewer, matching the
  // reference product's dedicated live-view tab rather than an in-app modal.
  { path: '/sessions/:sessionId/live', element: <LiveViewPage /> },
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'sessions', element: <SessionsListPage /> },
      { path: 'sessions/:sessionId', element: <SessionDetailPage /> },
      {
        path: 'playground',
        element: <PlaygroundPage />,
        children: [
          { index: true, element: <Navigate to="scrape" replace /> },
          { path: 'scrape', element: <PlaygroundScrapeTab /> },
          { path: 'map', element: <PlaygroundMapTab /> },
          { path: 'crawl', element: <PlaygroundCrawlTab /> },
          { path: 'interact', element: <PlaygroundInteractTab /> },
        ],
      },
      { path: 'api-keys', element: <ApiKeysPage /> },
      { path: 'nodes', element: <NodesPage /> },
    ],
  },
])
