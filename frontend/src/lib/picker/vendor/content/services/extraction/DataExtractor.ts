/* eslint-disable @typescript-eslint/no-explicit-any */
import { SchemaGenerator } from './SchemaGenerator';
import { generateNodeName } from './nodeName';
import { hashString } from '../../../shared/utils/hash';

/**
 * Extracts structured data from DOM elements
 * Enhanced with hash-based deduplication
 */

export class DataExtractor {
    private schemaGenerator = new SchemaGenerator();

    public extract(element: HTMLElement, containerInfo: any) {
        if (containerInfo.isContainer) {
            return this.extractList(containerInfo);
        } else {
            return this.extractSingle(element);
        }
    }

    private extractList(containerInfo: any) {
        const { siblings, confidence } = containerInfo;
        const rows: any[] = [];
        const seenHashes = new Set<string>();
        let duplicateCount = 0;

        siblings.forEach((child: HTMLElement) => {
            const rowData = this.extractRow(child);
            const hash = this.hashData(rowData);

            if (seenHashes.has(hash)) {
                duplicateCount++;
            } else {
                seenHashes.add(hash);
                rows.push(rowData);
            }
        });

        // Generate columns using SchemaGenerator (Reference Logic)
        const schemaResult = this.schemaGenerator.generateSchema(rows);
        const columns = schemaResult.columns;

        // Map raw rows to new schema IDs (normalized data)
        const items = this.normalizeData(rows, columns);

        return {
            type: 'list',
            columns: columns,
            items: items,
            source_url: window.location.href,
            count: items.length,
            timestamp: new Date().toISOString(),
            metadata: {
                containerConfidence: confidence || 0,
                duplicatesRemoved: duplicateCount,
                uniqueItems: items.length
            }
        };
    }

    private extractSingle(element: HTMLElement) {
        // [NEW] Smart Extraction for Single Elements (e.g., Buyboxes)
        // Instead of returning a single text blob, we extract the entire hierarchical DOM 
        // structure under this element, just like we do for a row in a list.
        const rowData = this.extractRow(element);

        // For single extraction, we create pseudo-columns from the keys
        const rows = [rowData];

        // Generate columns using SchemaGenerator so it looks identical to a list of 1
        const schemaResult = this.schemaGenerator.generateSchema(rows);
        const columns = schemaResult.columns;

        // Map raw rows to new schema IDs
        const items = this.normalizeData(rows, columns);

        return {
            type: 'list', // [CRITICAL] Fake it as a 'list' so PageExtractionStrategy explodes the columns
            columns: columns,
            items: items, // Return normalized items (array of 1)
            source_url: window.location.href,
            timestamp: new Date().toISOString()
        };
    }

    public extractRow(element: HTMLElement) {
        // Special case: <dt>/<dd> row pattern (e.g. <dl> wrapped in divs)
        // Both <dt> and <dd> often share the same first CSS class, causing their paths to
        // collide in processNode and the value to be lost. Handle them as Term/Value pairs.
        const dt = element.querySelector(':scope > dt') as HTMLElement | null;
        const dds = Array.from(element.querySelectorAll(':scope > dd')) as HTMLElement[];
        if (dt && dds.length > 0) {
            return this.extractDefinitionRow(dt, dds);
        }

        const rowData: any = {};
        this.processNode(element, [], {}, rowData);
        return rowData;
    }

    private extractDefinitionRow(dt: HTMLElement, dds: HTMLElement[]): any {
        const term = (dt.textContent || '').trim().replace(/:\s*$/, '');
        const values = dds.map(dd => this.extractDdText(dd)).filter(Boolean);
        return { Term: term, Value: values.join('; ') };
    }

    private extractDdText(dd: HTMLElement): string {
        // Prefer full aria-label from an expandable paragraph (avoids truncated visible text)
        const expandable = dd.querySelector('p[aria-label]') as HTMLElement | null;
        if (expandable) {
            const label = expandable.getAttribute('aria-label')?.trim();
            if (label) return label;
        }
        // Get text content excluding button labels
        const parts: string[] = [];
        const walk = (node: Node) => {
            if (node.nodeType === Node.TEXT_NODE) {
                const t = (node.textContent || '').trim();
                if (t) parts.push(t);
            } else if (node.nodeType === Node.ELEMENT_NODE) {
                const tag = (node as Element).tagName;
                if (tag === 'BUTTON' || tag === 'SCRIPT' || tag === 'STYLE') return;
                node.childNodes.forEach(walk);
            }
        };
        dd.childNodes.forEach(walk);
        return parts.join(' ').trim();
    }

    private processNode(t: HTMLElement, parentPath: string[], parentCounts: any, results: any) {
        if (!t || !t.tagName || ["SCRIPT", "STYLE", "NOSCRIPT", "SVG", "PATH"].includes(t.tagName)) return;

        // Check visibility via cheap inline style checks + offsetParent
        let isHidden = (t.offsetParent === null && t.offsetHeight === 0 && t.offsetWidth === 0)
            || t.style.display === 'none'
            || t.style.visibility === 'hidden';

        // display:contents wrappers (0x0, offsetParent null) render through their children
        if (isHidden && t.style.display !== 'none' && t.style.visibility !== 'hidden'
            && t.childElementCount > 0 && getComputedStyle(t).display === 'contents') {
            isHidden = false;
        }

        // Fully skip if hidden AND has no useful content or attributes
        if (isHidden) {
            const hasText = (t.textContent || '').trim().length > 0;
            const hasAttr = t.tagName === 'IMG' || t.tagName === 'A' || !!t.getAttribute('aria-label') || !!t.getAttribute('title');
            if (!hasText && !hasAttr) return;
        }

        // Skip elements with no offset parent and no text (display:none ancestors catch this)
        if (!isHidden && t.offsetParent === null && !(t.textContent || '').trim()) return;

        const displayLabel = generateNodeName(t);

        // Update counts for this label in the parent's context
        parentCounts[displayLabel] = (parentCounts[displayLabel] || 0) + 1;
        const count = parentCounts[displayLabel];

        // Construct path component e.g. "Div > Span (2)"
        // This effectively hashes the position so unrelated spans don't merge
        const pathComponent = `${displayLabel}${count > 1 ? ` (${count})` : ""}`;
        const currentPath = [...parentPath, pathComponent];
        const key = currentPath.join(" > ");

        // If hidden, extract only attributes (src, href, aria-label, alt, title) — no text, no recursion
        if (isHidden) {
            this.extractAttributes(t, key, results);
            return;
        }

        // 1. Extract Text
        let directText = "";
        try {
            if (t.childNodes) {
                directText = Array.from(t.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => n.textContent?.trim())
                    .filter(Boolean)
                    .join(" ");
            }
        } catch (e) { console.error('Error extracting text', e); }

        if (directText && (directText.length > 1 || /\d/.test(directText))) {
            results[key] = directText;
        }

        // [IMPROVED] Always check for aria-label or title as they are priority context,
        // even if directText was found.
        const ariaLabel = t.getAttribute('aria-label');
        const title = t.getAttribute('title');
        if (ariaLabel && ariaLabel.trim()) results[`${key} label`] = ariaLabel.trim();
        if (title && title.trim()) results[`${key} title`] = title.trim();

        // 2. Extract Links and Images
        this.extractLinkFields(t, key, results);
        this.extractImgFields(t, key, results);

        // 4. Recurse children
        const children = Array.from(t.children) as HTMLElement[];
        if (children.length > 0) {
            const currentCounts = {}; // Counts for children of t
            for (const child of children) {
                this.processNode(child, currentPath, currentCounts, results);
            }
        }
    }

    /**
     * Extract only key attributes from a hidden element (no text, no recursion).
     * Captures: src, href, aria-label, title, alt
     */
    private extractAttributes(t: HTMLElement, key: string, results: any) {
        // Extract text content from the hidden element itself (prices, reviews, ratings, etc.)
        const text = (t.textContent || '').trim();
        if (text && text.length > 0 && text.length < 500) {
            results[key] = text;
        }

        const ariaLabel = t.getAttribute('aria-label');
        const title = t.getAttribute('title');
        if (ariaLabel && ariaLabel.trim()) results[`${key} label`] = ariaLabel.trim();
        if (title && title.trim()) results[`${key} title`] = title.trim();

        this.extractLinkFields(t, key, results);
        this.extractImgFields(t, key, results);
    }

    private extractLinkFields(t: HTMLElement, key: string, results: any) {
        if (t.tagName !== 'A') return;
        const href = t.getAttribute('href');
        if (href && !href.trim().toLowerCase().startsWith('javascript:') && !href.startsWith('#')) {
            results[`${key} href`] = this.normalizeUrl(href.trim());
        }
    }

    private extractImgFields(t: HTMLElement, key: string, results: any) {
        if (t.tagName !== 'IMG') return;
        let src = t.getAttribute('src');
        if (!src) src = t.getAttribute('data-src');
        if (!src) src = t.getAttribute('data-lazy-src');

        if (src && !src.startsWith('data:')) {
            results[`${key} src`] = this.normalizeUrl(src.trim());
            if (t instanceof HTMLImageElement) {
                const area = (t.naturalWidth || t.width || 0) * (t.naturalHeight || t.height || 0);
                if (area > 0) {
                    results[`${key} src dimensions`] = {
                        width: t.naturalWidth || t.width,
                        height: t.naturalHeight || t.height,
                        area
                    };
                }
            }
        }

        const alt = t.getAttribute('alt');
        if (alt?.trim()) results[`${key} alt`] = alt.trim();
    }

    private normalizeUrl(t: string): string {
        if (!t) return "";
        if (t.trim().toLowerCase().startsWith("javascript:")) return "";
        if (t.startsWith("http://") || t.startsWith("https://")) return t;
        if (t.startsWith("//")) return `${window.location.protocol}${t}`;

        const { protocol, host, pathname } = window.location;
        const origin = `${protocol}//${host}`;

        if (t.startsWith("/")) return `${origin}${t}`;

        const pathParts = pathname.split("/").filter(Boolean);
        pathParts.pop();
        return `${origin}${pathParts.length > 0 ? `/${pathParts.join("/")}/` : "/"}${t}`;
    }

    public normalizeData(rows: any[], columns: any[]) {
        return rows.map(row => this.normalizeRow(row, columns));
    }

    /**
     * Normalizes a single raw row using the generated columns.
     * Maps original raw keys (paths) to their merged column IDs.
     */
    public normalizeRow(row: any, columns: any[]) {
        const newRow: any = {};
        columns.forEach(col => {
            if (col.type === 'image_array' && col.mergedKeys) {
                // Collect all sibling image values into an array
                const images: string[] = [];
                for (const key of col.mergedKeys) {
                    const val = row[key];
                    if (val && typeof val === 'string') images.push(val);
                }
                if (images.length > 0) newRow[col.id] = JSON.stringify(images);
            } else {
                const keys = col.sourceKeys || [col.originalId];
                for (const key of keys) {
                    const val = row[key];
                    if (val !== undefined && val !== null && val !== '') {
                        newRow[col.id] = val;
                        break;
                    }
                }
            }
        });
        return newRow;
    }

    /**
     * Generate deterministic hash from object for deduplication
     */
    private hashData(data: any): string {
        const sorted = this.sortObjectKeys(data);
        return hashString(JSON.stringify(sorted)).toString();
    }

    /**
     * Deep sort object keys for consistent hashing
     */
    private sortObjectKeys(obj: any): any {
        if (typeof obj !== 'object' || obj === null) {
            return obj;
        }

        if (Array.isArray(obj)) {
            return obj.map(item => this.sortObjectKeys(item));
        }

        return Object.keys(obj)
            .sort()
            .reduce((result: any, key) => {
                result[key] = this.sortObjectKeys(obj[key]);
                return result;
            }, {});
    }
}
