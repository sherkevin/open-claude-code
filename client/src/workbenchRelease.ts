import { useEffect, useState } from 'react';

function entryDocument(doc: Document) {
  return Array.from(doc.querySelectorAll<HTMLScriptElement>('script[type="module"][src]'))
    .map(script => script.getAttribute('src'))
    .find(src => src && /\/assets\/index-[\w.-]+\.js$/.test(src));
}

export function useWorkbenchUpdate() {
  const [updated, setUpdated] = useState(false);
  useEffect(() => {
    const running = entryDocument(document);
    if (!running) return;
    const lifetime = new AbortController();
    let pending = false;
    const check = async () => {
      if (pending || document.visibilityState === 'hidden') return;
      pending = true;
      try {
        const response = await fetch(window.location.pathname, {
          cache: 'no-store', mode: 'same-origin', signal: lifetime.signal,
          headers: { Accept: 'text/html' },
        });
        if (!response.ok || !response.headers.get('Content-Type')?.includes('text/html')) return;
        const latest = entryDocument(new DOMParser().parseFromString(await response.text(), 'text/html'));
        if (latest && latest !== running && !lifetime.signal.aborted) setUpdated(true);
      } catch {
        // The workspace stays usable during connection loss.
      } finally {
        pending = false;
      }
    };
    void check();
    window.addEventListener('focus', check);
    const timer = window.setInterval(check, 60_000);
    return () => {
      lifetime.abort();
      window.removeEventListener('focus', check);
      window.clearInterval(timer);
    };
  }, []);
  return updated;
}
