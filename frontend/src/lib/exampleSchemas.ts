// Reference JSON Schemas offered in the Playground so a user's first
// extraction is a working one they can edit, rather than a blank textarea and
// a guess at what the field wants.
//
// The Zara product page is the deliberate choice for the default: a retail PDP
// exercises every shape a real extraction needs -- a nested object, an array of
// objects, an enum, and genuinely optional fields -- so reading it teaches the
// schema language faster than prose does. The `description` on each field is
// not decoration: it is the instruction the model actually follows when it has
// to decide which of several numbers on the page is "the price".

export interface ExampleSchema {
  id: string
  label: string
  description: string
  prompt: string
  schema: Record<string, unknown>
}

export const ZARA_PDP_SCHEMA: Record<string, unknown> = {
  type: 'object',
  properties: {
    name: {
      type: 'string',
      description: 'Product name exactly as shown on the page, without the brand prefix.',
    },
    reference: {
      type: 'string',
      description: 'Manufacturer reference / style code, e.g. "3641/044/800".',
    },
    colour: { type: 'string', description: 'Name of the currently selected colour.' },
    price: {
      type: 'object',
      description: 'Current selling price and the pre-discount price when one is shown.',
      properties: {
        amount: { type: 'number', description: 'Current price as a number, no currency symbol.' },
        currency: { type: 'string', description: 'ISO 4217 code, e.g. GBP, EUR, USD.' },
        original_amount: {
          type: 'number',
          description: 'Struck-through price before discount. Omit when not on sale.',
        },
      },
      required: ['amount', 'currency'],
    },
    sizes: {
      type: 'array',
      description: 'Every size offered, including the ones that are sold out.',
      items: {
        type: 'object',
        properties: {
          label: { type: 'string', description: 'Size label as shown, e.g. "S", "EU 38".' },
          availability: {
            type: 'string',
            enum: ['in_stock', 'low_stock', 'out_of_stock'],
            description: 'Map "coming soon"/"notify me" to out_of_stock.',
          },
        },
        required: ['label', 'availability'],
      },
    },
    composition: {
      type: 'array',
      description: 'Fabric composition lines, e.g. "78% viscose".',
      items: { type: 'string' },
    },
    care_instructions: {
      type: 'array',
      description: 'Washing and care instructions, one per entry.',
      items: { type: 'string' },
    },
    description: { type: 'string', description: 'Marketing description paragraph.' },
    image_urls: {
      type: 'array',
      description: 'Absolute URLs of the product images.',
      items: { type: 'string' },
    },
  },
  // Only what a product page always has. Everything else is optional -- the
  // model answers null rather than inventing a value when the page omits it.
  required: ['name', 'price'],
}

export const ZARA_PDP_PROMPT =
  'Extract the product details from this Zara product page. Use only the currently ' +
  'selected colour variant. Prices are numbers without a currency symbol.'

export const EXAMPLE_SCHEMAS: ExampleSchema[] = [
  {
    id: 'zara-pdp',
    label: 'Zara product page (PDP)',
    description:
      'Nested object, array of objects, enum, optional fields — the shapes most extractions need.',
    prompt: ZARA_PDP_PROMPT,
    schema: ZARA_PDP_SCHEMA,
  },
  {
    id: 'product-list',
    label: 'Product listing (array at the root)',
    description:
      'A schema whose root is an array. The result comes back as a JSON array, not an object.',
    prompt: 'Extract every product tile in the listing grid.',
    schema: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          price: { type: 'number', description: 'Number only, no currency symbol.' },
          url: { type: 'string', description: 'Absolute URL of the product page.' },
        },
        required: ['name', 'price'],
      },
    },
  },
  {
    id: 'article',
    label: 'Article / blog post',
    description: 'A flat object — the simplest useful shape.',
    prompt: 'Extract the article metadata.',
    schema: {
      type: 'object',
      properties: {
        title: { type: 'string' },
        author: { type: 'string' },
        published_at: { type: 'string', description: 'ISO 8601 date, e.g. 2026-08-28.' },
        tags: { type: 'array', items: { type: 'string' } },
      },
      required: ['title'],
    },
  },
]

export const DEFAULT_EXAMPLE_SCHEMA = EXAMPLE_SCHEMAS[0]
