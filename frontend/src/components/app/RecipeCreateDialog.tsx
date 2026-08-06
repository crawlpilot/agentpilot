import { useState } from 'react'
import { Plus } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { JsonTextareaField } from '@/components/app/JsonTextareaField'
import { useCreateRecipe } from '@/hooks/useRecipes'
import { useToast } from '@/components/ui/toast'

const SCHEMA_PLACEHOLDER = `{
  "title": { "type": "scalar", "description": "product title" },
  "variants": {
    "type": "array",
    "description": "one row per size",
    "item_schema": {
      "size": { "type": "scalar", "description": "size label" },
      "price": { "type": "scalar", "description": "price for this size" }
    }
  }
}`

export function RecipeCreateDialog({ onCreated }: { onCreated: (recipeId: string, buildRunId: string) => void }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [fieldSchema, setFieldSchema] = useState<Record<string, unknown> | null>(null)
  const [intervalSeconds, setIntervalSeconds] = useState<number | null>(null)
  const createRecipe = useCreateRecipe()
  const { toast } = useToast()

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim() || !url.trim() || !fieldSchema) return
    createRecipe.mutate(
      {
        name: name.trim(),
        url: url.trim(),
        field_schema: fieldSchema,
        schedule_interval_seconds: intervalSeconds,
      },
      {
        onSuccess: (resp) => {
          setOpen(false)
          setName('')
          setUrl('')
          setFieldSchema(null)
          setIntervalSeconds(null)
          onCreated(resp.recipe_id, resp.build_run_id)
        },
        onError: (err) => toast({ title: 'Create failed', description: err.message, variant: 'destructive' }),
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus className="size-4" />
          New recipe
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Create recipe</DialogTitle>
          <DialogDescription>
            The agent explores the URL once to build reusable field locators, then replays them deterministically.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="recipe-name">Name</Label>
            <Input
              id="recipe-name"
              placeholder="zara-product"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="recipe-url">URL</Label>
            <Input
              id="recipe-url"
              placeholder="https://example.com/product/123"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          </div>
          <JsonTextareaField
            label="Field schema"
            value={fieldSchema}
            onChange={setFieldSchema}
            placeholder={SCHEMA_PLACEHOLDER}
            rows={10}
          />
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="recipe-interval">Schedule interval (seconds)</Label>
            <Input
              id="recipe-interval"
              type="number"
              min={1}
              placeholder="leave blank for on-demand only"
              value={intervalSeconds ?? ''}
              onChange={(e) => setIntervalSeconds(e.target.value ? Number(e.target.value) : null)}
            />
          </div>
          <DialogFooter>
            <Button type="submit" disabled={createRecipe.isPending || !name.trim() || !url.trim() || !fieldSchema}>
              {createRecipe.isPending ? 'Creating…' : 'Create'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
