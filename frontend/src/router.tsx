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
import { PlaygroundAgentTab } from '@/routes/PlaygroundAgentTab'
import { AgentRunsListPage } from '@/routes/AgentRunsListPage'
import { AgentRunDetailPage } from '@/routes/AgentRunDetailPage'
import { RecipesListPage } from '@/routes/RecipesListPage'
import { MarketplacePage } from '@/routes/MarketplacePage'
import { MarketplaceDetailPage } from '@/routes/MarketplaceDetailPage'
import { RecipeJobPage } from '@/routes/RecipeJobPage'
import { RecipeDetailPage } from '@/routes/RecipeDetailPage'
import { RecipeWizardPage } from '@/routes/RecipeWizardPage'
import { RecipeStudioPage } from '@/routes/RecipeStudioPage'
import { ApiKeysPage } from '@/routes/ApiKeysPage'
import { NodesPage } from '@/routes/NodesPage'

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  // Standalone, no sidebar/topbar shell -- meant to be opened in its own
  // tab (see SessionsTable's "Live" link) as a focused viewer, matching the
  // reference product's dedicated live-view tab rather than an in-app modal.
  { path: '/sessions/:sessionId/live', element: <LiveViewPage /> },
  // The wizard is the front door: pick from the page, get a valid document.
  // The studio behind it is three panes wide -- a live page, the output schema
  // and the recipe document, all needed at once. Both get the full viewport,
  // for the same reason the live view does and by the same precedent.
  { path: '/recipes/new', element: <RecipeWizardPage /> },
  { path: '/recipes/new/studio', element: <RecipeStudioPage /> },
  { path: '/recipes/:recipeId/wizard', element: <RecipeWizardPage /> },
  { path: '/recipes/:recipeId/studio', element: <RecipeStudioPage /> },
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
          { path: 'agent', element: <PlaygroundAgentTab /> },
        ],
      },
      // Recipes are persistent, revisitable entities (like Sessions), not
      // one-shot Playground tools -- top-level list+detail, not a tab.
      { path: 'recipes', element: <RecipesListPage /> },
      { path: 'recipes/:recipeId', element: <RecipeDetailPage /> },
      // The marketplace is the *use* surface for the same objects `/recipes`
      // authors: browse what is published, hand one your URLs, read the batch.
      // Separate paths because they answer different questions -- "what did I
      // build?" against "what can I run?" -- and a published recipe belonging
      // to another tenant has no page under `/recipes` at all.
      { path: 'marketplace', element: <MarketplacePage /> },
      { path: 'marketplace/:recipeId', element: <MarketplaceDetailPage /> },
      { path: 'marketplace/:recipeId/jobs/:jobId', element: <RecipeJobPage /> },
      // Agent runs are persistent, revisitable entities (like Recipes/Sessions):
      // a top-level list + shareable detail, not just the one-shot Playground tab.
      { path: 'agent-runs', element: <AgentRunsListPage /> },
      { path: 'agent-runs/:runId', element: <AgentRunDetailPage /> },
      { path: 'api-keys', element: <ApiKeysPage /> },
      { path: 'nodes', element: <NodesPage /> },
    ],
  },
])
