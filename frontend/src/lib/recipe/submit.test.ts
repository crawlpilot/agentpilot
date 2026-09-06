import { describe, it, expect } from 'vitest'
import { parseUrls, rawPieceCount, suspiciousUrls } from './submit'

describe('parseUrls', () => {
  it('splits on newlines, commas and stray whitespace', () => {
    // All three turn up in real pastes -- a spreadsheet column, a CSV cell, a
    // chat message -- and "one per line" gets ignored regardless.
    expect(parseUrls('https://a.test/1\nhttps://a.test/2, https://a.test/3')).toEqual([
      'https://a.test/1',
      'https://a.test/2',
      'https://a.test/3',
    ])
  })

  it('drops duplicates, keeping the order they were pasted in', () => {
    expect(parseUrls('https://a.test/2 https://a.test/1 https://a.test/2')).toEqual([
      'https://a.test/2',
      'https://a.test/1',
    ])
  })

  it('is empty for whitespace only', () => {
    expect(parseUrls('   \n\n  ')).toEqual([])
    expect(parseUrls('')).toEqual([])
  })
})

describe('suspiciousUrls', () => {
  it('flags a missing scheme, which the driver cannot navigate to', () => {
    expect(suspiciousUrls(['example.com/p/1', 'https://a.test/1'])).toEqual(['example.com/p/1'])
  })

  it('accepts http as well as https', () => {
    expect(suspiciousUrls(['http://a.test/1', 'HTTPS://a.test/2'])).toEqual([])
  })

  it('does not try to judge anything else', () => {
    // A typo'd host or a 404 is only knowable by fetching it; guessing would
    // reject URLs that work.
    expect(suspiciousUrls(['https://definitely-not-a-real-host.invalid/x'])).toEqual([])
  })
})

describe('rawPieceCount', () => {
  it('counts what was pasted, so "duplicates removed" is only said when true', () => {
    const raw = 'https://a.test/1 https://a.test/1'
    expect(rawPieceCount(raw)).toBe(2)
    expect(parseUrls(raw)).toHaveLength(1)
  })

  it('is zero for empty input, so nothing is claimed about an empty box', () => {
    expect(rawPieceCount('   ')).toBe(0)
    expect(parseUrls('   ').length < rawPieceCount('   ')).toBe(false)
  })
})
