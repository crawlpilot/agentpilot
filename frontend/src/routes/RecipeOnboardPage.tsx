import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Plus, Sparkles, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { useOnboardRecipe } from '@/hooks/useRecipes'
import { useToast } from '@/components/ui/toast'
import { OnboardProgress } from '@/components/app/onboard/OnboardProgress'

/**
 * The front door: a URL, a sentence about what you want, and the agent builds
 * the recipe.
 *
 * The wizard next door is the other way in and stays -- it is the right tool
 * when you already know the page and want to point at things yourself. This one
 * is for the case where you do not, and it is the one that makes a catalogue of
 * scrapers something you can grow by describing rather than by clicking.
 *
 * Deliberately two fields. Everything else the recipe needs -- the field types,
 * the URL matcher, the reveal steps, the fallback chains, the assertions -- is
 * derived, and asking for any of it here would be asking the person to do the
 * work they came to avoid.
 */
export function RecipeOnboardPage() {
  const navigate = useNavigate()
  const { toast } = useToast()
  const onboard = useOnboardRecipe()

  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [mode, setMode] = useState<'describe' | 'schema'>('describe')
  const [description, setDescription] = useState('')
  const [schemaText, setSchemaText] = useState('')
  const [extraUrls, setExtraUrls] = useState<string[]>([])
  const [reviewFirst, setReviewFirst] = useState(false)
  const [started, setStarted] = useState<{ recipeId: string; runId: string } | null>(null)

  // A schema is only usable once it parses, so the button reflects that
  // rather than letting someone submit and find out from the worker.
  const parsedSchema = useMemo(() => {
    if (mode !== 'schema' || !schemaText.trim()) return null
    try {
      const value = JSON.parse(schemaText)
      return value && typeof value === 'object' && !Array.isArray(value)
        ? (value as Record<string, unknown>)
        : null
    } catch {
      return null
    }
  }, [mode, schemaText])
  const schemaError = mode === 'schema' && schemaText.trim().length > 0 && parsedSchema === null

  const canStart =
    url.trim().length > 0 &&
    (mode === 'describe' ? description.trim().length > 0 : parsedSchema !== null)

  function start() {
    onboard.mutate(
      {
        name: name.trim() || hostOf(url) || 'new recipe',
        url: url.trim(),
        description: mode === 'describe' ? description.trim() : undefined,
        output_schema: mode === 'schema' ? (parsedSchema ?? undefined) : undefined,
        sample_urls: extraUrls.map((u) => u.trim()).filter(Boolean),
        mode: reviewFirst ? 'assisted' : undefined,
      },
      {
        onSuccess: (resp) => setStarted({ recipeId: resp.recipe_id, runId: resp.run_id }),
        onError: (err) =>
          toast({ title: 'Could not start', description: err.message, variant: 'destructive' }),
      },
    )
  }

  if (started) {
    return (
      <OnboardProgress
        recipeId={started.recipeId}
        runId={started.runId}
        onDone={() => navigate(`/recipes/${started.recipeId}/wizard`)}
      />
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 p-6">
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={() => navigate('/recipes')}>
          <ArrowLeft className="size-4" />
          Recipes
        </Button>
      </div>

      <div>
        <h1 className="text-xl font-semibold">Build a scraper</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Point it at a page and say what you want off it. It reads the page, works out where each
          value lives, checks what it collected against the page, and asks you about anything it
          could not settle.
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="url">Page to build against</Label>
        <Input
          id="url"
          placeholder="https://shop.example.com/products/some-product"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <Label>What do you want from pages like this?</Label>
          <div className="ml-auto flex rounded-md border border-border p-0.5">
            <Button
              size="sm"
              variant={mode === 'describe' ? 'secondary' : 'ghost'}
              className="h-6 px-2 text-xs"
              onClick={() => setMode('describe')}
            >
              Describe it
            </Button>
            <Button
              size="sm"
              variant={mode === 'schema' ? 'secondary' : 'ghost'}
              className="h-6 px-2 text-xs"
              onClick={() => setMode('schema')}
            >
              Output schema
            </Button>
          </div>
        </div>

        {mode === 'describe' ? (
          <>
            <Textarea
              id="description"
              rows={4}
              placeholder={
                'product name, price, all the image URLs, and the sizes with whether each is in stock'
              }
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Plain English, one thing per line or comma-separated. Say the type when it matters
              &mdash; &ldquo;price&rdquo;, &ldquo;image URLs&rdquo;, &ldquo;a list of&hellip;&rdquo;
              &mdash; that is what tells it which element actually holds the value.
            </p>
          </>
        ) : (
          <>
            <Textarea
              id="schema"
              rows={12}
              className="font-mono text-xs"
              placeholder={SCHEMA_PLACEHOLDER}
              value={schemaText}
              onChange={(e) => setSchemaText(e.target.value)}
            />
            {schemaError ? (
              <p className="text-xs text-destructive">
                That is not valid JSON yet. Paste a JSON Schema, or an example of the object you
                want back.
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Either a JSON Schema (<code>{'{"type":"object","properties":{…}}'}</code>) or a
                plain example of the JSON you want back. Read exactly as written &mdash; no model
                reinterprets it &mdash; so this is the way to pin the output shape your downstream
                code already expects.
              </p>
            )}
          </>
        )}
      </div>

      <div className="flex items-start gap-2 rounded-md border border-border p-3">
        <input
          id="review-first"
          type="checkbox"
          className="mt-0.5 size-3.5 shrink-0"
          checked={reviewFirst}
          onChange={(e) => setReviewFirst(e.target.checked)}
        />
        <div className="flex flex-col gap-1">
          <Label htmlFor="review-first" className="cursor-pointer">
            Let me check the values before it saves
          </Label>
          <p className="text-xs text-muted-foreground">
            It normally only stops for fields it could not settle. Tick this and it stops even when
            everything bound, so you see what each field actually read. Worth it on a page whose own
            JSON carries a sponsored competitor under the same key names &mdash; every field
            resolves, one of them is the wrong product, and nothing mechanical catches that.
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <Label>More pages of the same kind (optional)</Label>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setExtraUrls((prev) => [...prev, ''])}
          >
            <Plus className="size-3.5" />
            Add
          </Button>
        </div>
        {extraUrls.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            With one URL it has to guess which parts of the address vary. With two it can see the
            shared shape, so the recipe declares what it applies to instead of accepting anything.
          </p>
        ) : (
          extraUrls.map((value, index) => (
            <div key={index} className="flex items-center gap-2">
              <Input
                placeholder="https://shop.example.com/products/another-product"
                value={value}
                onChange={(e) =>
                  setExtraUrls((prev) => prev.map((u, i) => (i === index ? e.target.value : u)))
                }
              />
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setExtraUrls((prev) => prev.filter((_, i) => i !== index))}
              >
                <Trash2 className="size-4" />
              </Button>
            </div>
          ))
        )}
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="name">Name (optional)</Label>
        <Input
          id="name"
          placeholder={hostOf(url) || 'e.g. example.com product'}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>

      <div className="flex items-center gap-3">
        <Button onClick={start} disabled={!canStart || onboard.isPending}>
          <Sparkles className="size-4" />
          {onboard.isPending ? 'Starting…' : 'Build it'}
        </Button>
        <span className="text-xs text-muted-foreground">
          Takes a few minutes &mdash; it drives a real browser through the page.
        </span>
      </div>
    </div>
  )
}

const SCHEMA_PLACEHOLDER = `{
  "type": "object",
  "required": ["name", "price"],
  "properties": {
    "name":   { "type": "string", "description": "the product title" },
    "price":  { "type": "number" },
    "images": { "type": "array", "items": { "type": "string", "format": "uri" } },
    "sizes":  {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "size":     { "type": "string" },
          "in_stock": { "type": "boolean" }
        }
      }
    }
  }
}`

function hostOf(url: string): string {
  try {
    return new URL(url).hostname
  } catch {
    return ''
  }
}
