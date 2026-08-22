/**
 * The jsdom shims React Flow needs before it will render anything at all.
 *
 * Without them the canvas mounts at zero size and renders no nodes — and every
 * "is this node type placeable" assertion then passes against an empty
 * container. A vacuously green suite is worse than a red one here, because it
 * is checking an explicit acceptance criterion, so src/test/environment.test.tsx
 * renders a real <ReactFlow> and fails loudly if these shims stop working.
 */
import "@testing-library/jest-dom/vitest";

import { afterEach } from "vitest";

/** Roughly a laptop viewport; React Flow only needs it to be non-zero. */
const PANE = { width: 1200, height: 800 };

class ResizeObserverStub implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

class DOMMatrixStub {
  m22 = 1;
  constructor(readonly transform?: string) {}
}

globalThis.ResizeObserver ??= ResizeObserverStub;
// @xyflow/system reads DOMMatrixReadOnly to unpick the pane's CSS transform.
(globalThis as Record<string, unknown>)["DOMMatrixReadOnly"] ??= DOMMatrixStub;

globalThis.matchMedia ??= ((query: string) =>
  ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }) as unknown as MediaQueryList) as typeof globalThis.matchMedia;

// jsdom lays nothing out, so every box is 0×0 and React Flow concludes the pane
// is not on screen yet. Report the pane's size for the pane and a plausible card
// for everything else; the numbers only have to be non-zero and stable.
Object.defineProperty(HTMLElement.prototype, "getBoundingClientRect", {
  configurable: true,
  value(this: HTMLElement): DOMRect {
    const isPane = this.classList.contains("react-flow__pane") || this.classList.contains("react-flow");
    const size = isPane ? PANE : { width: 220, height: 96 };
    return {
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: size.width,
      bottom: size.height,
      ...size,
      toJSON: () => ({}),
    } as DOMRect;
  },
});

Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, value: PANE.width });
Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: PANE.height });

// jsdom has no layout engine, so React Flow's drag handlers throw on it.
(Element.prototype as unknown as Record<string, unknown>)["setPointerCapture"] ??= () => {};
(Element.prototype as unknown as Record<string, unknown>)["releasePointerCapture"] ??= () => {};
(Element.prototype as unknown as Record<string, unknown>)["hasPointerCapture"] ??= () => false;

afterEach(() => {
  document.body.innerHTML = "";
});
