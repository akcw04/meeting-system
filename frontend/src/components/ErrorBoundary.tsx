import { Component, type ErrorInfo, type ReactNode } from "react";

/** Catches render-time errors in the page below it and shows a recoverable
 * message instead of a blank white screen. The top-bar nav is kept OUTSIDE this
 * boundary, and the boundary is re-keyed per view in App, so switching tabs (or
 * Reload) recovers without a dead end. */
export default class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surface it in the console for debugging; the UI shows the message below.
    console.error("Unhandled UI error:", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="error-boundary">
          <h2>Something went wrong on this page</h2>
          <p>
            Your meetings and data are safe — this is just a display error.
            Switch tabs or reload to continue.
          </p>
          <pre>{this.state.error.message}</pre>
          <button onClick={() => window.location.reload()}>Reload</button>
        </div>
      );
    }
    return this.props.children;
  }
}
