/**
 * The last line of defence for the island.
 *
 * templates/flows/edit.html covers the case where the bundle is *missing*, but
 * not the case where it loads and then fails: without a boundary, a throw
 * during render unmounts the tree and leaves the server-rendered "Loading the
 * flow builder…" text on screen for good, with the reason only in the console.
 *
 * The same component renders a bootstrap failure — a malformed mount div, a
 * schema the bundle cannot read — so there is one place that turns "the island
 * broke" into something a person can act on.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export function BuilderFailure({ error }: { error: Error }) {
  return (
    <div className="h-full flex items-center justify-center p-8">
      <div className="alert-error max-w-lg">
        <p className="font-medium">The flow builder stopped working.</p>
        <p className="mt-1">
          Reload the page. If it keeps happening, this message helps whoever looks into it:
        </p>
        <p className="fb-problem-code mt-1">{error.message}</p>
      </div>
    </div>
  );
}

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Kept for whoever opens the console: the rendered copy is deliberately
    // short, and the component stack is the part that locates the fault.
    console.error("The flow builder crashed.", error, info.componentStack);
  }

  override render(): ReactNode {
    return this.state.error ? <BuilderFailure error={this.state.error} /> : this.props.children;
  }
}
