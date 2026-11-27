import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { setFinancialDatabaseContext } from './api/localSession';
import {
  clearPending, executePortfolio, loadPortfolios, preparePortfolio, readPending, reconcilePortfolio, storePending,
} from './api/portfolioManagement';
import type { Command, Pending, Portfolio, PortfolioDirectory, TicketResult } from './api/portfolioManagement';

export function initialPortfolio(portfolios: Portfolio[], configured: string | undefined) {
  if (configured !== undefined) {
    const value = configured.trim();
    const id = /^[1-9][0-9]*$/.test(value) ? Number(value) : null;
    const valid = Number.isSafeInteger(id) && portfolios.some(row => row.id === id);
    return { id: valid ? id : null, invalidConfiguration: !valid };
  }
  return { id: portfolios.find(row => row.id === 1)?.id ?? (portfolios.length === 1 ? portfolios[0].id : null),
    invalidConfiguration: false };
}

type Mutation = { pending: boolean; error: string; success: string; outcome: TicketResult['outcome'] | null };
const idle: Mutation = { pending: false, error: '', success: '', outcome: null };
const uncertain = 'The previous change may have saved. Check its status before submitting another change.';
const storageError = 'Tab storage is unavailable or has an invalid pending marker. Portfolio changes are disabled in this tab.';

type Context = {
  directory: PortfolioDirectory | null;
  active: Portfolio | undefined;
  select: (id: number | null) => void;
  refresh: () => Promise<boolean>;
  loading: boolean;
  error: string;
  invalidConfiguration: boolean;
  mutation: Mutation;
  canManage: boolean;
  pending: Pending | null;
  storageProblem: string;
  submit: (input: Command, id: number | null) => Promise<boolean>;
  reconcile: (close: boolean) => Promise<void>;
  acknowledge: () => void;
};

const PortfolioContext = createContext<Context | null>(null);

export function usePortfolios() {
  const value = useContext(PortfolioContext);
  if (!value) throw new Error('Portfolio context is unavailable.');
  return value;
}

export function PortfolioProvider({ children, configured }: { children: ReactNode; configured?: string }) {
  const [directory, setDirectory] = useState<PortfolioDirectory | null>(null);
  const [id, setId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [invalidConfiguration, setInvalidConfiguration] = useState(false);
  const [mutation, setMutation] = useState(idle);
  const [pending, setPending] = useState<Pending | null>(null);
  const [storageProblem, setStorageProblem] = useState('');
  const selected = useRef<number | null>(null);
  const directoryFresh = useRef(false);
  const currentDirectory = useRef<PortfolioDirectory | null>(null);
  const initialContext = useRef<string | null>(null);
  const listing = useRef<AbortController | null>(null);
  const saving = useRef<AbortController | null>(null);
  const unresolved = useRef<Pending | null>(null);
  const selectCreated = useRef<{ context: string; id: number } | null>(null);

  const select = useCallback((next: number | null) => {
    selectCreated.current = null;
    selected.current = next;
    setId(next);
  }, []);

  const refresh = useCallback(async () => {
    listing.current?.abort();
    const request = new AbortController();
    listing.current = request;
    directoryFresh.current = false;
    setLoading(true);
    try {
      const next = await loadPortfolios(request.signal);
      if (request.signal.aborted) return false;
      const created = selectCreated.current;
      selectCreated.current = null;
      const createdId = created?.context === next.context && next.portfolios.some(row => row.id === created.id)
        ? created.id : null;
      if (initialContext.current !== next.context) {
        const first = initialContext.current === null;
        const initial = first ? initialPortfolio(next.portfolios, configured) : { id: null, invalidConfiguration: false };
        initialContext.current = next.context;
        setInvalidConfiguration(initial.invalidConfiguration);
        select(createdId ?? initial.id);
      } else if (createdId !== null) {
        select(createdId);
      } else if (selected.current !== null && !next.portfolios.some(row => row.id === selected.current)) {
        select(null);
      }
      setFinancialDatabaseContext(next.context);
      currentDirectory.current = next;
      directoryFresh.current = true;
      setDirectory(next);
      setError('');
      return true;
    } catch (failure) {
      if (!request.signal.aborted) setError(failure instanceof Error ? failure.message : 'Unable to load portfolios.');
      return false;
    } finally {
      if (listing.current === request) {
        listing.current = null;
        setLoading(false);
      }
    }
  }, [configured, select]);

  useEffect(() => {
    try {
      const marker = readPending(sessionStorage);
      unresolved.current = marker;
      setPending(marker);
      if (marker) setMutation({ ...idle, error: uncertain });
    } catch {
      setStorageProblem(storageError);
    }
    void refresh();
    return () => {
      listing.current?.abort();
      listing.current = null;
      saving.current?.abort();
      saving.current = null;
      setFinancialDatabaseContext(null);
    };
  }, [refresh]);

  function clear(marker: Pending) {
    if (unresolved.current?.ticket !== marker.ticket) return false;
    try {
      if (!clearPending(sessionStorage, marker.ticket)) throw new Error('Pending marker changed.');
    } catch {
      setStorageProblem(storageError);
      return false;
    }
    unresolved.current = null;
    setPending(null);
    return true;
  }

  function record(marker: Pending, result: TicketResult) {
    if (unresolved.current?.ticket !== marker.ticket) return false;
    if (result.outcome === 'committed' && result.portfolio) {
      if (marker.target === null) {
        selectCreated.current = { context: marker.context, id: result.portfolio.id };
      }
      clear(marker);
      setMutation({ ...idle, success: marker.target === null ? 'Portfolio created.' : 'Portfolio name saved.' });
      void refresh();
      return true;
    }
    if (result.outcome === 'not_started' || result.outcome === 'rolled_back') {
      clear(marker);
      setMutation({ ...idle, error: result.reason ?? 'The portfolio change did not take effect.' });
      return false;
    }
    setMutation({ ...idle, outcome: result.outcome, error: result.reason ?? uncertain });
    return false;
  }

  const canManage = !!directory?.management_configured && directory.write_availability === 'available' && !error && !loading;

  async function submit(input: Command, target: number | null) {
    if (!directory || !canManage || saving.current || unresolved.current || storageProblem) return false;
    const request = new AbortController();
    saving.current = request;
    setMutation({ ...idle, pending: true });
    try {
      const marker = await preparePortfolio(input, target, directory, request.signal);
      if (request.signal.aborted) return false;
      if (currentDirectory.current?.context !== marker.context || !directoryFresh.current) {
        setMutation({ ...idle, error: 'The portfolio directory changed. Review it before submitting again.' });
        return false;
      }
      try {
        storePending(sessionStorage, marker);
      } catch {
        setStorageProblem(storageError);
        setMutation(idle);
        return false;
      }
      unresolved.current = marker;
      setPending(marker);
      const result = await executePortfolio(input, marker, request.signal);
      return !request.signal.aborted && record(marker, result);
    } catch (failure) {
      if (!request.signal.aborted) setMutation({ ...idle, error: unresolved.current ? uncertain :
        failure instanceof Error ? failure.message : 'The portfolio change could not be prepared.' });
      return false;
    } finally {
      if (saving.current === request) saving.current = null;
    }
  }

  async function reconcile(close: boolean) {
    const marker = unresolved.current;
    if (!marker || saving.current) return;
    const request = new AbortController();
    saving.current = request;
    setMutation(previous => ({ ...previous, pending: true }));
    try {
      // A changed generation fences the old ticket. A changed file alone does not.
      if (directory && directory.generation !== marker.generation) {
        setMutation({ ...idle, outcome: 'unavailable', error: 'The backend restarted. The previous change may have saved.' });
        return;
      }
      let result = await reconcilePortfolio(marker, close, request.signal);
      for (let attempt = 0; !close && result.outcome === 'running' && attempt < 3; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 300));
        request.signal.throwIfAborted();
        result = await reconcilePortfolio(marker, false, request.signal);
      }
      if (!request.signal.aborted) record(marker, result);
    } catch {
      if (!request.signal.aborted) setMutation(previous => ({ ...previous, pending: false, error: uncertain }));
    } finally {
      if (saving.current === request) saving.current = null;
    }
  }

  function acknowledge() {
    const marker = unresolved.current;
    if (!marker || saving.current || !['unavailable', 'unknown'].includes(mutation.outcome ?? '')) return;
    if (clear(marker)) setMutation({ ...idle, success: 'Unresolved outcome acknowledged.' });
  }

  return <PortfolioContext.Provider value={{
    directory, active: directory?.portfolios.find(row => row.id === id), select, refresh, loading,
    error, invalidConfiguration, mutation, submit, canManage, pending, storageProblem, reconcile, acknowledge,
  }}>{children}</PortfolioContext.Provider>;
}
