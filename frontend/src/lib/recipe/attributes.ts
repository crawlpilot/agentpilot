/**
 * What a locator reads off the element it resolves to.
 *
 * Shared rather than duplicated because the choice has to be offerable
 * everywhere a person can bind a field, and it was not. The wizard had this
 * list and the studio had a shorter copy of it; the assist panel — the one
 * place where somebody is correcting a binding the model got wrong — had
 * neither, so a manually picked `<a>` could only ever be bound as its text. The
 * classifier's guess was the only attribute the human-in-the-loop flow could
 * express.
 *
 * Pair with `setReadAttribute` in `./fromPick`, which rewrites a candidate
 * chain to read a different attribute.
 */
export interface ReadAttribute {
  value: string
  label: string
  hint: string
}

export const READ_ATTRIBUTES: ReadAttribute[] = [
  {
    value: 'text',
    label: 'Text',
    hint: 'The element’s text, excluding script/style. Includes text present but not painted.',
  },
  {
    value: 'visible_text',
    label: 'Visible text',
    hint: 'Only what is rendered — excludes collapsed content.',
  },
  { value: 'href', label: 'Link (href)', hint: 'The link target.' },
  { value: 'src', label: 'Image (src)', hint: 'The image source URL.' },
  {
    value: 'value',
    label: 'Form value',
    hint: 'The current value of an input, select or textarea.',
  },
  {
    value: 'html',
    label: 'HTML',
    hint: 'Outer HTML — only when the markup itself is the data.',
  },
  {
    value: 'title',
    label: 'title',
    hint: 'Often the full text when the visible label is truncated.',
  },
  { value: 'alt', label: 'alt', hint: 'An image’s alt text.' },
  { value: 'content', label: 'content', hint: 'As used by meta tags.' },
  {
    value: 'datetime',
    label: 'datetime',
    hint: 'A <time> element’s machine-readable timestamp.',
  },
]

/** The hint for one attribute, for a help line under the chooser. */
export function attributeHint(value: string): string {
  return READ_ATTRIBUTES.find((a) => a.value === value)?.hint ?? ''
}
