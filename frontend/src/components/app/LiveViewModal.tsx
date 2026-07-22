import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { Dialog, DialogContent } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { LiveViewPanel } from '@/components/app/LiveViewPanel'

export function LiveViewModal({
  sessionId,
  open,
  onOpenChange,
}: {
  sessionId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent showClose={false} className="flex h-[85vh] w-[90vw] max-w-5xl flex-col p-0">
        <LiveViewPanel
          sessionId={sessionId}
          toolbarEnd={
            <DialogPrimitive.Close asChild>
              <Button size="icon" variant="ghost">
                <X className="size-4" />
              </Button>
            </DialogPrimitive.Close>
          }
        />
      </DialogContent>
    </Dialog>
  )
}
