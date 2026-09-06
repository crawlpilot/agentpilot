import { VisualElementPicker } from '../VisualElementPicker';

export interface ISelectionStrategy {
    handleHover(element: HTMLElement, picker: VisualElementPicker): void;
    handleClick(element: HTMLElement, picker: VisualElementPicker, sendResponse: (msg: any) => void): void;

    preferredAction?: 'extract' | 'click';
}
