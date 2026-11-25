import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { invalidateLocalSession, onSessionLost, verifyLocalSession } from './api/localSession';

export function LocalSessionGate({ initial, children }: { initial: Promise<string>; children: ReactNode }) {
  const [state, setState] = useState<'checking' | 'ready' | 'locked'>('checking');
  useEffect(() => {
    let disposed = false;
    let locked = false;
    let checking = false;
    let generation: string | undefined;
    const lock = () => {
      locked = true;
      if (!disposed) setState('locked');
    };
    const unsubscribe = onSessionLost(lock);
    void initial.then(value => {
      if (!disposed && !locked) {
        generation = value;
        setState('ready');
      }
    }, () => {
      if (!disposed) {
        invalidateLocalSession();
        lock();
      }
    });
    const verify = async () => {
      if (disposed || locked || checking || !generation) return;
      checking = true;
      try {
        await verifyLocalSession(generation);
      } catch {
        if (!disposed) invalidateLocalSession();
      } finally {
        checking = false;
      }
    };
    const timer = window.setInterval(() => void verify(), 30000);
    window.addEventListener('focus', verify);
    window.addEventListener('pageshow', verify);
    return () => {
      disposed = true;
      unsubscribe();
      window.clearInterval(timer);
      window.removeEventListener('focus', verify);
      window.removeEventListener('pageshow', verify);
    };
  }, [initial]);

  if (state === 'ready') return children;
  return <main className="workspace">
    {state === 'checking'
      ? <p role="status">Checking local session…</p>
      : <section role="alert">
          <h1>Local session unavailable</h1>
          <p>Refresh if the launcher recently opened a new session. Otherwise stop it, run dev.cmd again, and use the browser tab it opens.</p>
        </section>}
  </main>;
}
