import { describe, it, expect } from 'vitest';
import { isUnstableClass, isHashClass, isStableAttributeValue, isVolatileSelector, filterPersistableChain } from './stability';

describe('isUnstableClass', () => {
    it('flags Tailwind utility classes', () => {
        expect(isUnstableClass('w-4')).toBe(true);
        expect(isUnstableClass('flex')).toBe(true);
        expect(isUnstableClass('bg-red-500')).toBe(true);
        expect(isUnstableClass('hover:underline')).toBe(true);
        expect(isUnstableClass('[--border-width:1px]')).toBe(true);
    });
    it('flags CSS-in-JS hash classes', () => {
        expect(isUnstableClass('css-dy3i81')).toBe(true);
        expect(isUnstableClass('sc-abc12')).toBe(true);
    });
    it('flags versioned/state classes', () => {
        expect(isUnstableClass('layout-ss26')).toBe(true);
        expect(isUnstableClass('active')).toBe(true);
    });
    it('allows semantic classes through', () => {
        expect(isUnstableClass('product-card')).toBe(false);
        expect(isUnstableClass('article-title')).toBe(false);
        expect(isUnstableClass('nav-item')).toBe(false);
    });
});

describe('isHashClass', () => {
    it('matches emotion/styled-components hash patterns', () => {
        expect(isHashClass('css-dy3i81')).toBe(true);
        expect(isHashClass('sc-bdvaja')).toBe(true);
    });
    it('rejects semantic classes', () => {
        expect(isHashClass('product-card')).toBe(false);
        expect(isHashClass('css')).toBe(false);
    });
});

describe('isStableAttributeValue', () => {
    it('rejects always-instance-specific attributes', () => {
        expect(isStableAttributeValue('data-id', '12345')).toBe(false);
        expect(isStableAttributeValue('href', '/product/123')).toBe(false);
    });
    it('accepts vocabulary attributes regardless of value', () => {
        expect(isStableAttributeValue('role', 'button')).toBe(true);
    });
    it('accepts stable test-id values, rejects instance-shaped ones', () => {
        expect(isStableAttributeValue('data-testid', 'submit-button')).toBe(true);
        expect(isStableAttributeValue('data-testid', 'item-123456')).toBe(false);
    });
    it('rejects values with digits or URL characters for generic attrs', () => {
        expect(isStableAttributeValue('name', 'field-42')).toBe(false);
        expect(isStableAttributeValue('name', 'https://x.com')).toBe(false);
    });
    it('accepts short, stable, lowercase generic attribute values', () => {
        expect(isStableAttributeValue('name', 'search-field')).toBe(true);
    });
});

describe('isVolatileSelector', () => {
    it('flags positional and hash-class selectors', () => {
        expect(isVolatileSelector('div:nth-child(2)')).toBe(true);
        expect(isVolatileSelector('.css-dy3i81')).toBe(true);
        expect(isVolatileSelector('.sc-abc12 > span')).toBe(true);
    });
    it('allows structural/semantic selectors', () => {
        expect(isVolatileSelector('.product-card')).toBe(false);
        expect(isVolatileSelector('[data-testid="price"]')).toBe(false);
    });
});

describe('filterPersistableChain', () => {
    it('drops volatile entries when a stable entry exists', () => {
        const chain = [
            { selector: '.css-dy3i81', strategy: 'CSS Gen' },
            { selector: '.product-card', strategy: 'Minimal' },
            { selector: 'div:nth-child(3)', strategy: 'Path' },
        ];
        expect(filterPersistableChain(chain)).toEqual([{ selector: '.product-card', strategy: 'Minimal' }]);
    });
    it('keeps the whole chain when every entry is volatile', () => {
        const chain = [{ selector: '.css-dy3i81', strategy: 'CSS Gen' }, { selector: 'div:nth-child(3)', strategy: 'Path' }];
        expect(filterPersistableChain(chain)).toEqual(chain);
    });
    it('passes through empty/undefined chains unchanged', () => {
        expect(filterPersistableChain(undefined)).toBeUndefined();
        expect(filterPersistableChain([])).toEqual([]);
    });
});
