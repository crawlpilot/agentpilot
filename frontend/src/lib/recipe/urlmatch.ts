// Mirrors `agentpilot/recipe/v2/urlmatch.py` so the studio's URL tester gives
// the same answer the worker will.
//
// The semantics that matter and are easy to get wrong: `glob` is Python's
// `fnmatch`, which anchors at both ends and lets `*` cross a `/`; `regex` is
// a *search*, not a full match; `host` matches the host itself or any
// subdomain of it. A malformed pattern declines the URL rather than throwing --
// a stored recipe with a bad regex should not take down whatever loads it.

import type { TargetSpec, UrlMatcher } from './types'

function globToRegExp(pattern: string): RegExp {
  let out = ''
  for (let i = 0; i < pattern.length; i += 1) {
    const ch = pattern[i]
    if (ch === '*') out += '.*'
    else if (ch === '?') out += '.'
    else if (ch === '[') {
      const close = pattern.indexOf(']', i + 1)
      if (close === -1) {
        out += '\\['
      } else {
        let set = pattern.slice(i + 1, close)
        i = close
        if (set.startsWith('!')) set = `^${set.slice(1)}`
        out += `[${set.replace(/\\/g, '\\\\')}]`
      }
    } else out += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&')
  }
  return new RegExp(`^${out}$`)
}

export function matcherMatches(matcher: UrlMatcher, url: string): boolean {
  try {
    if (matcher.kind === 'glob') return globToRegExp(matcher.pattern).test(url)
    if (matcher.kind === 'host') {
      const host = new URL(url).hostname.toLowerCase()
      // Python's `lstrip("*.")` strips any run of leading `*` and `.`, so
      // "*.zara.com" and ".zara.com" both mean the same host.
      const want = matcher.pattern.toLowerCase().replace(/^[*.]+/, '')
      return host === want || host.endsWith(`.${want}`)
    }
    if (matcher.kind === 'regex') return new RegExp(matcher.pattern).test(url)
  } catch {
    return false
  }
  return false
}

export function targetAccepts(target: TargetSpec, url: string): boolean {
  if (target.match.length === 0) return true
  return target.match.some((m) => matcherMatches(m, url))
}
