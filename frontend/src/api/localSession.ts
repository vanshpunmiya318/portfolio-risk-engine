const endpoint = '/api/local-session';
const authorization = new EventTarget();
let financialContext: string | null = null;
let sessionRequests: AbortController | undefined;

export function setFinancialDatabaseContext(context: string | null) {
  financialContext = context;
}

export function protectLocalSessionRequests() {
  sessionRequests ??= new AbortController();
}

export function invalidateLocalSession() {
  if (sessionRequests?.signal.aborted) return;
  sessionRequests?.abort();
  authorization.dispatchEvent(new Event('lost'));
}

export function onSessionLost(callback: () => void) {
  authorization.addEventListener('lost', callback);
  return () => authorization.removeEventListener('lost', callback);
}

export async function apiFetch(path: string, options: RequestInit = {}) {
  const sessionSignal = sessionRequests?.signal;
  sessionSignal?.throwIfAborted();
  const signal = sessionSignal && options.signal
    ? AbortSignal.any([sessionSignal, options.signal]) : sessionSignal ?? options.signal;
  const context = (!options.method || options.method === "GET") && /^\/api\/portfolios?\/(?!mutations(?:\/|$))/.test(path) ? financialContext : null;
  const headers = context ? new Headers(options.headers) : options.headers;
  if (context && headers instanceof Headers) headers.set('X-RiskEngine-Database-Context', context);
  const response = await fetch(path, { ...options, headers, signal, credentials: 'same-origin' });
  if (context && (financialContext !== context || response.headers.get('X-RiskEngine-Context-Changed') === '1')) {
    if (financialContext === context) invalidateLocalSession();
    throw new Error('The portfolio database context changed.');
  }
  if (response.status === 401) invalidateLocalSession();
  else sessionSignal?.throwIfAborted();
  return response;
}

export async function checkLocalSession(): Promise<string> {
  const response = await apiFetch(endpoint, {
    cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) throw new Error('Local session unavailable.');
  const body = await response.json();
  sessionRequests?.signal.throwIfAborted();
  if (typeof body.generation !== 'string' || !body.generation) throw new Error('Invalid local session response.');
  return body.generation;
}

export async function verifyLocalSession(generation: string) {
  if (await checkLocalSession() !== generation) invalidateLocalSession();
}

export async function initializeLocalSession(location: Location, history: History): Promise<string> {
  if (location.hash.startsWith('#launch=')) {
    const code = location.hash.slice('#launch='.length);
    history.replaceState(history.state, '', location.pathname + location.search);
    const response = await apiFetch(endpoint, {
      method: 'POST', headers: { 'X-RiskEngine-Bootstrap': code },
      cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(5000),
    });
    if (response.status !== 204) throw new Error('Local session unavailable.');
  }
  return checkLocalSession();
}
