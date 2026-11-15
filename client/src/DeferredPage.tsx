import { Component, lazy, Suspense, useMemo, useState, type ComponentType, type ReactNode } from 'react';
import { RefreshCw } from 'lucide-react';
import { Button, ErrorNotice, Loader } from './components';

/** Reuse successful/pending imports; a failed request must remain retryable. */
export function retryablePageLoader<T>(load: () => Promise<T>) {
  let pending: Promise<T> | undefined;
  return () => {
    if (!pending) pending = Promise.resolve().then(load).catch(error => {
      pending = undefined;
      throw error;
    });
    return pending;
  };
}

class PageLoadBoundary extends Component<{ children: ReactNode; onRetry: () => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return <div className="page">
      <ErrorNotice message="页面暂未打开，请重试。也可以先从左侧进入其他页面。"/>
      <Button variant="secondary" onClick={this.props.onRetry}><RefreshCw size={16}/>重新打开</Button>
    </div>;
    return this.props.children;
  }
}

/** Keep navigation available while an unopened page downloads or needs a retry. */
export function deferredPage<Props extends object>(load: () => Promise<{ default: ComponentType<Props> }>) {
  const loadPage = retryablePageLoader(load);
  return function DeferredPage(props: Props) {
    const [attempt, setAttempt] = useState(0);
    // Lazy keeps the imported component's props; the cast avoids React's generic ref union.
    const Page = useMemo(() => lazy(loadPage), [attempt]) as unknown as ComponentType<Props>;
    return <PageLoadBoundary key={attempt} onRetry={() => setAttempt(value => value + 1)}>
      <Suspense fallback={<div className="page" role="status" aria-live="polite"><Loader text="正在打开页面…"/></div>}>
        <Page {...props}/>
      </Suspense>
    </PageLoadBoundary>;
  };
}
