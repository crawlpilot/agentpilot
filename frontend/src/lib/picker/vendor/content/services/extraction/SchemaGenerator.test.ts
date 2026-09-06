import { describe, it, expect } from 'vitest';
import { SchemaGenerator } from './SchemaGenerator';

describe('SchemaGenerator.generateSchema', () => {
    it('keeps positionally-indexed keys as separate columns when they co-occur with different values', () => {
        // LinkedIn-style rows: obfuscated classes make every sibling label identical,
        // so name/headline/location only differ by the (N) counter.
        const rows = [
            {
                'div > div > p': 'Nitin Kewlani',
                'div > div > p (2)': 'Senior Engineer at Target',
                'div > div > p (3)': 'Bengaluru, Karnataka, India'
            },
            {
                'div > div > p': 'Ronit S.',
                'div > div > p (2)': 'Talent Acquisition @Rupeek',
                'div > div > p (3)': 'Bengaluru, Karnataka, India'
            }
        ];

        const { columns } = new SchemaGenerator().generateSchema(rows);
        expect(columns).toHaveLength(3);
        const ids = new Set(columns.map(c => c.id));
        expect(ids.size).toBe(3);
    });

    it('still coalesces indexed keys that never co-occur (row variants of the same field)', () => {
        const rows = [
            { 'div > span': 'Alpha' },
            { 'div > span (2)': 'Beta' } // e.g. an ad badge shifted the position in this row
        ];

        const { columns } = new SchemaGenerator().generateSchema(rows);
        expect(columns).toHaveLength(1);
        expect(columns[0].sourceKeys).toEqual(['div > span', 'div > span (2)']);
    });

    it('still merges sibling images into an image_array column', () => {
        const rows = [
            {
                'div > img src': 'https://a.com/1.jpg',
                'div > img (2) src': 'https://a.com/2.jpg'
            }
        ];

        const { columns } = new SchemaGenerator().generateSchema(rows);
        expect(columns).toHaveLength(1);
        expect(columns[0].type).toBe('image_array');
        expect(columns[0].mergedKeys).toEqual(['div > img src', 'div > img (2) src']);
    });
});
