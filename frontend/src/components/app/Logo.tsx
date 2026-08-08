import { cn } from '@/lib/utils'

// The product mark: a rounded accent-red tile with a stylized browser-window
// glyph, plus the wordmark. Kept as one component so the sidebar, the login
// brand panel, and any future marketing surface all render an identical logo
// instead of each hand-rolling a red dot + text.
export function Logo({
  className,
  showWordmark = true,
  // `inverted` renders for a dark/accent-red surface (the login brand panel):
  // a white tile with a red glyph and a white wordmark, instead of the
  // default red-tile-on-light treatment used in the sidebar.
  inverted = false,
}: {
  className?: string
  showWordmark?: boolean
  inverted?: boolean
}) {
  return (
    <div className={cn('flex items-center gap-2', className)}>
      <span
        className={cn(
          'grid size-7 shrink-0 place-items-center rounded-md shadow-xs',
          inverted ? 'bg-white text-accent' : 'bg-accent text-accent-foreground',
        )}
      >
        <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
          <rect x="3" y="4" width="18" height="16" rx="2.5" stroke="currentColor" strokeWidth="1.8" />
          <path d="M3 8.5h18" stroke="currentColor" strokeWidth="1.8" />
          <circle cx="6" cy="6.25" r="0.9" fill="currentColor" />
          <path d="M9.5 14.5l2.5-2.5 2.5 2.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
      {showWordmark && (
        <span className={cn('text-sm font-semibold tracking-tight', inverted ? 'text-white' : 'text-foreground')}>
          agentpilot
        </span>
      )}
    </div>
  )
}
