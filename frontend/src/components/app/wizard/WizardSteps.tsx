import { Check } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface WizardStep {
  id: string
  title: string
  /** Why this step exists, in the author's terms. */
  hint: string
}

interface Props {
  steps: WizardStep[]
  current: number
  /** Furthest step reached, so a completed step can be revisited. */
  furthest: number
  onGoTo: (index: number) => void
}

export function WizardSteps({ steps, current, furthest, onGoTo }: Props) {
  return (
    <ol className="flex shrink-0 items-center gap-1 overflow-x-auto px-3 py-2">
      {steps.map((step, index) => {
        const done = index < furthest
        const active = index === current
        return (
          <li key={step.id} className="flex items-center gap-1">
            <button
              type="button"
              disabled={index > furthest}
              onClick={() => onGoTo(index)}
              title={step.hint}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors',
                active && 'bg-accent text-accent-foreground',
                !active && index <= furthest && 'text-foreground hover:bg-muted',
                index > furthest && 'cursor-not-allowed text-muted-foreground/50',
              )}
            >
              <span
                className={cn(
                  'flex size-4 shrink-0 items-center justify-center rounded-full border text-[10px]',
                  active ? 'border-accent-foreground' : 'border-current',
                )}
              >
                {done ? <Check className="size-2.5" /> : index + 1}
              </span>
              {step.title}
            </button>
            {index < steps.length - 1 && <span className="text-muted-foreground/40">/</span>}
          </li>
        )
      })}
    </ol>
  )
}
