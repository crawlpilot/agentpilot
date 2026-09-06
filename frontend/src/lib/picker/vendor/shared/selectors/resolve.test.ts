import { describe, it, expect, beforeEach } from 'vitest';
import { resolveFirst, resolveAll, normalizeChain, looksLikeXPath } from './resolve';

describe('looksLikeXPath', () => {
    it('detects xpath-shaped strings', () => {
        expect(looksLikeXPath('//div[@id="x"]')).toBe(true);
        expect(looksLikeXPath('./div')).toBe(true);
        expect(looksLikeXPath('(//div)[1]')).toBe(true);
    });
    it('rejects css selectors', () => {
        expect(looksLikeXPath('.card')).toBe(false);
        expect(looksLikeXPath('div.item')).toBe(false);
    });
});

describe('normalizeChain', () => {
    it('normalizes strings, SelectorMetadata, and SelectorDefinition shapes', () => {
        const chain = normalizeChain([
            '.foo',
            { selector: '.bar', strategy: 'Minimal' },
            { value: '//div', type: 'xpath' },
            { value: '.baz', type: 'css' },
            null,
            undefined,
            { selector: '' },
        ]);
        expect(chain).toEqual([
            { value: '.foo', kind: 'css' },
            { value: '.bar', kind: 'css', strategy: 'Minimal' },
            { value: '//div', kind: 'xpath' },
            { value: '.baz', kind: 'css' },
        ]);
    });
});

describe('resolveFirst / resolveAll', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="root">
                <div class="card">A</div>
                <div class="card">B</div>
                <div class="card">C</div>
                <div class="stale-hash-xyz1">D</div>
            </div>
        `;
    });

    it('resolveFirst returns the first entry with a match', () => {
        const { element, used } = resolveFirst(['.nope', '.card'], document.body);
        expect(element?.textContent).toBe('A');
        expect(used?.value).toBe('.card');
    });

    it('resolveFirst falls back through the whole chain', () => {
        const { element } = resolveFirst(['.nope', '.also-nope', '.stale-hash-xyz1'], document.body);
        expect(element?.textContent).toBe('D');
    });

    it('resolveFirst returns null when nothing in the chain matches', () => {
        const { element, used } = resolveFirst(['.nope', '.still-nope'], document.body);
        expect(element).toBeNull();
        expect(used).toBeNull();
    });

    it('resolveFirst resolves xpath entries', () => {
        const { element } = resolveFirst(['//div[@class="stale-hash-xyz1"]'], document.body);
        expect(element?.textContent).toBe('D');
    });

    it('resolveAll returns all matches for the first working entry', () => {
        const { elements, used } = resolveAll(['.nope', '.card'], document.body);
        expect(elements).toHaveLength(3);
        expect(used?.value).toBe('.card');
    });

    it('resolveAll prefers the entry whose match count is closest to expectedCount', () => {
        document.body.innerHTML = `
            <div id="root">
                <div class="wide">1</div><div class="wide">2</div>
                <div class="narrow">1</div><div class="narrow">2</div><div class="narrow">3</div>
            </div>
        `;
        const { elements, used } = resolveAll(['.wide', '.narrow'], document.body, { expectedCount: 3 });
        expect(elements).toHaveLength(3);
        expect(used?.value).toBe('.narrow');
    });

    it('resolveAll returns empty when the chain is empty or unmatched', () => {
        expect(resolveAll([], document.body).elements).toEqual([]);
        expect(resolveAll(['.nope'], document.body).elements).toEqual([]);
    });
});
