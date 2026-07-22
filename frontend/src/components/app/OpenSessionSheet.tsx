import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useOpenSession } from '@/hooks/useSessionMutations'
import { useToast } from '@/components/ui/toast'
import type { Tier } from '@/lib/api/types'

export function OpenSessionSheet({ trigger }: { trigger?: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  const [domain, setDomain] = useState('')
  const [name, setName] = useState('')
  const [tier, setTier] = useState<Tier>('auto')
  const [headful, setHeadful] = useState(false)
  const openSession = useOpenSession()
  const { toast } = useToast()
  const navigate = useNavigate()

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!domain.trim() || !name.trim()) return
    openSession.mutate(
      { domain: domain.trim(), name: name.trim(), tier, headful, live_view: true },
      {
        onSuccess: (resp) => {
          toast({ title: 'Session opened', description: resp.session_id })
          setOpen(false)
          setDomain('')
          setName('')
          navigate(`/sessions/${resp.session_id}`)
        },
        onError: (err) => {
          toast({ title: 'Failed to open session', description: err.message, variant: 'destructive' })
        },
      },
    )
  }

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        {trigger ?? <Button>Open a session</Button>}
      </SheetTrigger>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>Open a session</SheetTitle>
          <SheetDescription>
            Starts (or reuses a warm) browser context for this identity and opens a live-view-ready
            session.
          </SheetDescription>
        </SheetHeader>
        <form className="flex flex-1 flex-col gap-4" onSubmit={handleSubmit}>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="domain">Domain</Label>
            <Input
              id="domain"
              placeholder="example.com"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              autoFocus
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="name">Identity name</Label>
            <Input
              id="name"
              placeholder="prod-crawler-1"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Tier</Label>
            <Select value={tier} onValueChange={(v) => setTier(v as Tier)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">auto</SelectItem>
                <SelectItem value="basic">basic</SelectItem>
                <SelectItem value="stealth">stealth</SelectItem>
                <SelectItem value="enhanced">enhanced</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={headful} onChange={(e) => setHeadful(e.target.checked)} />
            Headful (visible browser window on the node)
          </label>
          <SheetFooter>
            <Button type="submit" disabled={openSession.isPending}>
              {openSession.isPending ? 'Opening…' : 'Open session'}
            </Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  )
}
