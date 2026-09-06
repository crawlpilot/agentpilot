/**
 * Shared result type returned by all ContainerDetector implementations.
 */
export interface ContainerResult {
    isContainer: boolean;
    containerType?: 'list' | 'table';
    container: HTMLElement | null;
    childCount: number;
    siblings: HTMLElement[];
    confidence?: number;
    candidates?: any[];
}

/**
 * Interface for container detection strategies.
 * Implementations differ in how they identify repeating list containers.
 */
export interface IContainerDetector {
    findContainer(element: HTMLElement): ContainerResult;
    findInternalContainer(element: HTMLElement): ContainerResult;
}
