import { describe, it, expect } from 'vitest';
import { generateNodeName } from './nodeName';

function el(html: string): HTMLElement {
    const wrapper = document.createElement('div');
    wrapper.innerHTML = html;
    return wrapper.firstElementChild as HTMLElement;
}

describe('generateNodeName', () => {
    it('prefers a stable data-testid value over any class', () => {
        const node = el('<span class="css-1a2b3c4" data-testid="price">$19.99</span>');
        expect(generateNodeName(node)).toBe('price');
    });

    it('rejects instance-shaped data-testid values and falls through', () => {
        const node = el('<span class="product-price" data-testid="item-123456">$5</span>');
        expect(generateNodeName(node)).toBe('product-price');
    });

    it('uses the semantic tag when no stable attribute is present', () => {
        const node = el('<h2 class="sc-xyz999">Product Title</h2>');
        expect(generateNodeName(node)).toBe('h2');
    });

    it('falls back to the first non-volatile class', () => {
        const node = el('<span class="css-hash1 product-price">$5</span>');
        expect(generateNodeName(node)).toBe('product-price');
    });

    it('skips Tailwind utility and hash classes entirely', () => {
        const node = el('<span class="flex items-center text-red-500 css-hash1">Discount</span>');
        expect(generateNodeName(node)).toBe('span');
    });

    it('uses tag[role] when no usable class exists', () => {
        const node = el('<div class="css-hash1" role="button">Click</div>');
        expect(generateNodeName(node)).toBe('div[button]');
    });

    it('falls back to the bare tag as a last resort', () => {
        const node = el('<div class="css-hash1"></div>');
        expect(generateNodeName(node)).toBe('div');
    });

    it('skips obfuscated hex hash classes (LinkedIn-style atomic CSS)', () => {
        const node = el('<div class="_75228706 e6d1c6b3 _57a8c203 fb259d69" role="listitem">Item</div>');
        expect(generateNodeName(node)).toBe('div[listitem]');
    });
});
