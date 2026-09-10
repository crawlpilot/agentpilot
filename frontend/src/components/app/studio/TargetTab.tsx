import { useState } from 'react'
import { Check, Plus, Trash2, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { targetAccepts } from '@/lib/recipe/urlmatch'
import type { Recipe, UrlMatcher } from '@/lib/recipe/types'

/**
 * Which URLs this recipe will accept.
 *
 * `replay_recipe` calls `target_accepts` **before it opens a browser**, so this
 * one field decides whether a recipe is a reusable thing or a bookmark. It was
 * reachable only through the raw JSON tab, which meant a matcher derived from
 * one sample URL was a guess nobody could see — and the first time anyone found
 * out it was too narrow was when a perfectly good recipe refused their URL with
 * "url does not match this recipe's target".
 *
 * So the tester is the point of this tab, not a nicety. It runs
 * `lib/recipe/urlmatch.ts`, which mirrors `urlmatch.py` deliberately, so the
 * answer here is the answer the worker will give.
 */

const KIND_HELP: Record<UrlMatcher['kind'], string> = {
  glob: 'Wildcards. `*` matches anything, including `/` — the same as Python\'s fnmatch.',
  host: 'The host and any subdomain of it. Accepts every page on the site.',
  regex: 'A search, not a full match — it need only occur somewhere in the URL.',
}

interface Props {
  recipe: Recipe
  update: (fn: (r: Recipe) => Recipe) => void
}

export function TargetTab({ recipe, update }: Props) {
  const matchers = recipe.target?.match ?? []
  const [probe, setProbe] = useState(recipe.sample_urls?.[0] ?? '')

  function setMatchers(next: UrlMatcher[]) {
    update((r) => ({ ...r, target: { ...r.target, match: next } }))
  }

  const accepted = probe.trim() ? targetAccepts(recipe.target ?? {}, probe.trim()) : null

  return (
    <div className="flex flex-col gap-5">
      <section className="flex flex-col gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          URLs this recipe accepts
        </h3>
        <p className="text-[11px] text-muted-foreground">
          Checked before a browser is opened, so a URL that does not match is refused outright.
          Any one matcher accepting the URL accepts it.
        </p>

        {matchers.length === 0 && (
          <div className="rounded-md border border-warning/40 bg-warning/10 p-2 text-[11px]">
            No matcher, so this recipe accepts <b>any</b> URL. That is right for a generic recipe
            and wrong for one built against a particular site.
          </div>
        )}

        {matchers.map((matcher, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <Select
              value={matcher.kind}
              onValueChange={(kind) =>
                setMatchers(
                  matchers.map((m, j) =>
                    j === i ? { ...m, kind: kind as UrlMatcher['kind'] } : m,
                  ),
                )
              }
            >
              <SelectTrigger className="h-7 w-24 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(['glob', 'host', 'regex'] as const).map((k) => (
                  <SelectItem key={k} value={k} title={KIND_HELP[k]}>
                    {k}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input
              className="h-7 flex-1 font-mono text-xs"
              value={matcher.pattern}
              placeholder="https://example.com/product/*"
              onChange={(e) =>
                setMatchers(
                  matchers.map((m, j) => (j === i ? { ...m, pattern: e.target.value } : m)),
                )
              }
            />
            <Button
              size="sm"
              variant="ghost"
              className="size-7 shrink-0 p-0"
              onClick={() => setMatchers(matchers.filter((_, j) => j !== i))}
            >
              <Trash2 className="size-3.5" />
            </Button>
          </div>
        ))}

        {matchers[0] && (
          <p className="text-[11px] text-muted-foreground">{KIND_HELP[matchers[0].kind]}</p>
        )}

        <Button
          size="sm"
          variant="outline"
          className="h-7 w-fit px-2 text-[11px]"
          onClick={() => setMatchers([...matchers, { kind: 'glob', pattern: '' }])}
        >
          <Plus className="size-3.5" />
          Add a matcher
        </Button>
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Try a URL
        </h3>
        <p className="text-[11px] text-muted-foreground">
          Paste a page of the same kind — a different product, a different listing. If it is
          refused here, the recipe will refuse it too.
        </p>
        <Input
          className="h-7 font-mono text-xs"
          value={probe}
          placeholder="https://example.com/product/another-one/12345"
          onChange={(e) => setProbe(e.target.value)}
        />
        {accepted !== null && (
          <div className="flex items-center gap-1.5">
            {accepted ? (
              <Badge variant="success">
                <Check className="size-3" />
                accepted
              </Badge>
            ) : (
              <Badge variant="destructive">
                <X className="size-3" />
                refused
              </Badge>
            )}
            <span className="text-[11px] text-muted-foreground">
              {accepted
                ? 'This recipe would run against that page.'
                : 'Widen a matcher — most likely by replacing the part of the path that identifies one page with `*`.'}
            </span>
          </div>
        )}
      </section>
    </div>
  )
}
