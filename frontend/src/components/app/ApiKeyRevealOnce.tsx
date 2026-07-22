import { useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import type { ApiKeyCreateOut } from '@/lib/api/types'

export function ApiKeyRevealOnce({ apiKey, onClose }: { apiKey: ApiKeyCreateOut | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false)

  return (
    <Dialog open={apiKey !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Key created</DialogTitle>
          <DialogDescription>
            Copy this now — you won't be able to see it again. If you lose it, revoke it and create a new one.
          </DialogDescription>
        </DialogHeader>
        {apiKey && (
          <div className="flex items-center gap-2 rounded-md border border-border bg-muted p-3 font-mono text-sm">
            <span className="flex-1 select-all break-all">{apiKey.api_key}</span>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => {
                navigator.clipboard.writeText(apiKey.api_key)
                setCopied(true)
                setTimeout(() => setCopied(false), 1500)
              }}
            >
              {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
            </Button>
          </div>
        )}
        <DialogFooter>
          <Button onClick={onClose}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
