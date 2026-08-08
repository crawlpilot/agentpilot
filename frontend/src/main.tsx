import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'
// Self-hosted Inter (variable, optical-size axis) -- imported here so Vite
// bundles the .woff2 files; see the note in index.css for why it can't live
// in the stylesheet's @import chain.
import '@fontsource-variable/inter/opsz.css'
import './index.css'
import { queryClient } from '@/lib/query/queryClient'
import { AuthProvider } from '@/lib/auth/AuthContext'
import { ToastUIProvider } from '@/components/ui/toast'
import { TooltipProvider } from '@/components/ui/tooltip'
import { router } from './router'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ToastUIProvider>
          <TooltipProvider delayDuration={200}>
            <RouterProvider router={router} />
          </TooltipProvider>
        </ToastUIProvider>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
)
