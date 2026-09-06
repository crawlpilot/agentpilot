import { useState } from 'react'
import { Loader2, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useSessionsList } from '@/hooks/useSessionsList'
import { useOpenSession } from '@/hooks/useSessionMutations'
import { useToast } from '@/components/ui/toast'

interface Props {
  sessionId: string | null
  onSessionChange: (id: string) => void
  sampleUrls: string[]
  onSampleUrlsChange: (urls: string[]) => void
  name: string
  onNameChange: (name: string) => void
}

function hostOf(url: string): string | null {
  try {
    return new URL(url).hostname
  } catch {
    return null
  }
}

/**
 * Pick or create a browser session, and say which pages this recipe is for.
 *
 * Creating a session happens *here* rather than sending the author to
 * `/sessions` in another tab, which is what the studio does today
 * (`PagePane.tsx`'s empty state calls `window.open`). Requiring a detour
 * through an unrelated screen before the first useful action is most of why
 * the current studio feels like it has no on-ramp.
 */
export function Step1Session({
  sessionId,
  onSessionChange,
  sampleUrls,
  onSampleUrlsChange,
  name,
  onNameChange,
}: Props) {
  const { data } = useSessionsList()
  const openSession = useOpenSession()
  const { toast } = useToast()
  const [raw, setRaw] = useState(sampleUrls.join('\n'))

  const sessions = (data?.sessions ?? []).filter((s) => s.state === 'active')

  function commitUrls(text: string) {
    setRaw(text)
    onSampleUrlsChange(
      text
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean),
    )
  }

  function createSession() {
    // The domain a session is opened against has to match the pages it will
    // load, so derive it from the first sample URL rather than asking twice.
    const domain = sampleUrls.map(hostOf).find(Boolean)
    if (!domain) {
      toast({
        title: 'Add a sample URL first',
        description: 'The session is opened against that page’s domain.',
      })
      return
    }
    openSession.mutate(
      { domain, name: name || `recipe: ${domain}`, live_view: true },
      {
        onSuccess: (session) => onSessionChange(session.session_id),
        onError: (err) =>
          toast({ title: 'Could not open a session', description: err.message, variant: 'destructive' }),
      },
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-5 p-6">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="recipe-name">Recipe name</Label>
        <Input
          id="recipe-name"
          value={name}
          onChange={(e) => onNameChange(e.target.value)}
          placeholder="e.g. Zara product detail"
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sample-urls">Sample URLs</Label>
        <Textarea
          id="sample-urls"
          rows={4}
          value={raw}
          onChange={(e) => commitUrls(e.target.value)}
          placeholder={'https://example.com/p/1\nhttps://example.com/p/2\nhttps://example.com/p/3'}
          className="font-mono text-xs"
        />
        <p className="text-[11px] text-muted-foreground">
          One per line, and more than one on purpose. A recipe built from a single page is a guess
          fitted to that page &mdash; the second and third URL are what turn it into a pattern.
        </p>
      </div>

      <div className="flex flex-col gap-1.5">
        <Label>Browser session</Label>
        {sessions.length > 0 ? (
          <div className="flex items-center gap-2">
            <Select value={sessionId ?? ''} onValueChange={onSessionChange}>
              <SelectTrigger className="flex-1">
                <SelectValue placeholder="Pick a running session" />
              </SelectTrigger>
              <SelectContent>
                {sessions.map((s) => (
                  <SelectItem key={s.session_id} value={s.session_id}>
                    {s.name || s.session_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button variant="outline" onClick={createSession} disabled={openSession.isPending}>
              {openSession.isPending ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Plus className="size-3.5" />
              )}
              New
            </Button>
          </div>
        ) : (
          <div className="flex flex-col items-start gap-2 rounded-md border border-dashed border-border p-4">
            <p className="text-sm text-muted-foreground">
              No session running. You need one to load a page and pick against something real.
            </p>
            <Button onClick={createSession} disabled={openSession.isPending}>
              {openSession.isPending ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Plus className="size-3.5" />
              )}
              Open a session
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}
