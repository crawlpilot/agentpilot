import type { IContainerDetector, ContainerResult } from './IContainerDetector';
import { GroupParentContainerDetector } from './GroupParentContainerDetector';
import { FingerprintContainerDetector } from './FingerprintContainerDetector';

export type { ContainerResult };

/**
 * ContainerDetector — public facade for container detection.
 *
 * Delegates to a pluggable IContainerDetector implementation.
 * Default: GroupParentContainerDetector (robust, reference-based approach).
 * Alternative: FingerprintContainerDetector (original fingerprint-based approach).
 *
 * Usage:
 *   const detector = new ContainerDetector();                          // uses GroupParent (default)
 *   const detector = new ContainerDetector('fingerprint');             // uses Fingerprint
 *   const detector = new ContainerDetector(myCustomImplementation);   // uses custom
 */
export class ContainerDetector {
    private impl: IContainerDetector;

    constructor(strategy: 'groupParent' | 'fingerprint' | IContainerDetector = 'groupParent') {
        if (typeof strategy === 'object') {
            this.impl = strategy;
        } else if (strategy === 'fingerprint') {
            this.impl = new FingerprintContainerDetector();
        } else {
            this.impl = new GroupParentContainerDetector();
        }
    }

    public findContainer(element: HTMLElement): ContainerResult {
        return this.impl.findContainer(element);
    }

    public findInternalContainer(element: HTMLElement): ContainerResult {
        return this.impl.findInternalContainer(element);
    }
}

// Re-export implementations for direct use if needed
export { GroupParentContainerDetector } from './GroupParentContainerDetector';
export { FingerprintContainerDetector } from './FingerprintContainerDetector';
export type { IContainerDetector } from './IContainerDetector';
