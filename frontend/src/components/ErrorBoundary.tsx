import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Shown above the generic message, e.g. the surface's name. */
  label?: string;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Last-resort render-time guard.
 *
 * The app has no error boundary anywhere, so an unvalidated backend
 * payload that crashes deep in a render tree (e.g. a malformed
 * `GET /backtests/{id}/edge-decay` response reaching an unguarded
 * `.map` in `EdgeDecayTable`) currently white-screens the entire app,
 * not just the surface that received the bad data. This degrades that
 * failure to a readable message scoped to whatever it wraps.
 *
 * This is a backstop, not a substitute for validating payloads at the
 * fetch site — defensive checks belong there too (see `EdgeDecayTable`'s
 * `Array.isArray(report.rows)` guard).
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('ErrorBoundary caught a render error:', error, info.componentStack);
  }

  private reset = (): void => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      return (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-8 text-center">
          {this.props.label && (
            <p className="text-sm font-medium text-destructive">{this.props.label}</p>
          )}
          <p className="mt-1 text-muted-foreground">
            Something went wrong rendering this view. This is likely a malformed or
            unexpected server response, not a problem with your data.
          </p>
          <button
            onClick={this.reset}
            className="mt-4 rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-muted"
          >
            Try again
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
