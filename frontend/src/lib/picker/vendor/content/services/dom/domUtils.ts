import { SelectorGenerator } from './selectors/SelectorGenerator';
import type { SelectorResult, SelectorOptions } from './selectors/SelectorGenerator';
import { generateCssSelector, generateXPath } from './domBase';
import { sanitizeCssSelector } from '../../../shared/selectors/sanitize';

const generator = new SelectorGenerator();

export { generateCssSelector, generateXPath, sanitizeCssSelector };
export type { SelectorResult, SelectorOptions };

export function generateRobustSelectors(element: HTMLElement, options?: SelectorOptions): SelectorResult[] {
    return generator.generateAll(element, options);
}

/**
 * Robustly simulates a click by dispatching the full pointer/mouse event sequence.
 *
 * Yields between phases via queueMicrotask instead of setTimeout so that:
 * - React and other frameworks process each phase before the next one fires
 * - Background tabs are NOT affected (microtasks run at full speed; setTimeout
 *   is throttled to ≥1000ms per phase in hidden tabs)
 */
export async function clickElementRobust(element: HTMLElement): Promise<boolean> {
    const tick = () => new Promise<void>(r => queueMicrotask(r));

    try {
        const rect = element.getBoundingClientRect();
        const clientX = rect.left + rect.width / 2;
        const clientY = rect.top + rect.height / 2;

        const eventInit = {
            view: window,
            bubbles: true,
            cancelable: true,
            clientX,
            clientY,
            button: 0,
            buttons: 1,
            pointerId: 1,
            pointerType: 'mouse',
            isPrimary: true
        } as any;

        // 1. Entrance
        element.dispatchEvent(new MouseEvent('mouseover', eventInit));
        element.dispatchEvent(new MouseEvent('mouseenter', eventInit));
        element.dispatchEvent(new PointerEvent('pointerover', eventInit));
        element.dispatchEvent(new PointerEvent('pointerenter', eventInit));

        await tick();

        // 2. Press
        element.dispatchEvent(new PointerEvent('pointerdown', eventInit));
        element.dispatchEvent(new MouseEvent('mousedown', eventInit));
        if (element.focus) element.focus();

        await tick();

        // 3. Release
        element.dispatchEvent(new PointerEvent('pointerup', eventInit));
        element.dispatchEvent(new MouseEvent('mouseup', eventInit));

        await tick();

        // 4. Click
        element.dispatchEvent(new MouseEvent('click', eventInit));

        return true;
    } catch (e) {
        console.error('[domUtils] clickElementRobust failed:', e);
        return false;
    }
}
