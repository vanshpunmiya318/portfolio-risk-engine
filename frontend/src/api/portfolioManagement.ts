import { apiFetch } from './localSession';

export type Portfolio = { id: number; name: string; currency: string };
export type PortfolioDirectory = {
  portfolios: Portfolio[];
  unsupported: { id: string; reason: 'nonpositive' | 'outside_client_range' }[];
  context: string;
  generation: string;
  management_configured: boolean;
  write_availability: 'unavailable' | 'unknown' | 'available';
  supported_currencies: string[];
};
export type Command = { name: string; currency?: string };
export type Pending = { ticket: string; generation: string; context: string; target: number | null };
type Outcome = 'prepared' | 'running' | 'committed' | 'rolled_back' | 'not_started' | 'unknown' | 'unavailable';
export type TicketResult = { ticket: string; outcome: Outcome; portfolio: Portfolio | null; reason: string | null };

const directoryPath = '/api/portfolios';
const ticketPath = directoryPath + '/mutations';
const markerKey = 'riskengine.pendingPortfolioMutation';
const ticketPattern = /^[A-Za-z0-9_-]{43}$/;
const outcomes: Outcome[] = ['prepared', 'running', 'committed', 'rolled_back', 'not_started', 'unknown', 'unavailable'];

function isId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

function isPortfolio(value: unknown): value is Portfolio {
  if (!value || typeof value !== 'object') return false;
  const row = value as Portfolio;
  return isId(row.id) && typeof row.name === 'string' && typeof row.currency === 'string';
}

export function readPending(storage: Storage) {
  const raw = storage.getItem(markerKey);
  if (raw === null) return null;
  const marker = JSON.parse(raw) as Pending;
  if (!marker || typeof marker.ticket !== 'string' || !ticketPattern.test(marker.ticket) ||
      typeof marker.generation !== 'string' || !marker.generation || typeof marker.context !== 'string' ||
      !marker.context || (marker.target !== null && !isId(marker.target))) throw new Error('Invalid pending change marker.');
  return marker;
}

export function storePending(storage: Storage, marker: Pending) {
  if (readPending(storage)) throw new Error('A portfolio change is already pending.');
  storage.setItem(markerKey, JSON.stringify(marker));
}

export function clearPending(storage: Storage, ticket: string) {
  if (readPending(storage)?.ticket !== ticket) return false;
  storage.removeItem(markerKey);
  return true;
}

async function read(path: string, signal: AbortSignal, options: RequestInit = {}, pending?: Pending): Promise<unknown> {
  const deadline = AbortSignal.any([signal, AbortSignal.timeout(6000)]);
  const response = await apiFetch(path, { ...options, signal: deadline, cache: 'no-store', redirect: 'error' });
  const body = await response.json().catch(() => {
    deadline.throwIfAborted();
    if (path === directoryPath && !options.method) throw new Error('The portfolio list response was invalid.');
    if (path === ticketPath) throw new Error('The portfolio change could not be prepared. The server response was invalid.');
    throw new Error('The portfolio change response was invalid.');
  });
  deadline.throwIfAborted();
  if (!response.ok) {
    if (pending && body?.ticket === pending.ticket && body?.http_status === response.status &&
        ['rolled_back', 'not_started', 'unknown'].includes(body.outcome)) return body;
    if (path === directoryPath && !options.method) throw new Error('Unable to load portfolios. Refresh to try again.');
    if (path === ticketPath && response.status >= 500) throw new Error('The portfolio change could not be prepared. Try again.');
    throw new Error(typeof body?.detail === 'string' ? body.detail : 'The portfolio request failed. Check the inputs and try again.');
  }
  return body;
}

function write(method: 'POST' | 'PATCH', body?: object, ticket?: string): RequestInit {
  const headers = new Headers({ 'X-RiskEngine-Request': '1' });
  if (body) headers.set('Content-Type', 'application/json');
  if (ticket) headers.set('X-RiskEngine-Ticket', ticket);
  return { method, headers, body: body && JSON.stringify(body) };
}

export async function loadPortfolios(signal: AbortSignal): Promise<PortfolioDirectory> {
  const result = await read(directoryPath, signal) as PortfolioDirectory;
  if (!result || !Array.isArray(result.portfolios) || !result.portfolios.every(isPortfolio) ||
      new Set(result.portfolios.map(row => row.id)).size !== result.portfolios.length ||
      typeof result.context !== 'string' || !result.context || typeof result.generation !== 'string' || !result.generation ||
      typeof result.management_configured !== 'boolean' ||
      !['available', 'unavailable', 'unknown'].includes(result.write_availability) ||
      !Array.isArray(result.unsupported) || !result.unsupported.every(row =>
        row && typeof row.id === 'string' && /^-?\d+$/.test(row.id) && ['nonpositive', 'outside_client_range'].includes(row.reason)) ||
      !Array.isArray(result.supported_currencies) || !result.supported_currencies.every(code => typeof code === 'string')) {
    throw new Error('The portfolio list response was invalid.');
  }
  return result;
}

function validResult(value: unknown, pending?: Pending): TicketResult {
  const result = value as TicketResult;
  if (!result || typeof result.ticket !== 'string' || !ticketPattern.test(result.ticket) ||
      !outcomes.includes(result.outcome) || (result.reason !== null && typeof result.reason !== 'string') ||
      (pending && pending.ticket !== result.ticket) ||
      (result.outcome === 'committed'
        ? !isPortfolio(result.portfolio) || (pending?.target != null && result.portfolio.id !== pending.target)
        : result.portfolio !== null)) {
    throw new Error('The portfolio change response was invalid.');
  }
  return result;
}

export async function preparePortfolio(input: Command, target: number | null, directory: PortfolioDirectory, signal: AbortSignal) {
  const body = await read(ticketPath, signal, write('POST', { ...input, target, context: directory.context }));
  let result;
  try {
    result = validResult(body);
  } catch {
    throw new Error('The portfolio change could not be prepared. The server response was invalid.');
  }
  if (result.outcome !== 'prepared') throw new Error('The portfolio change could not be prepared.');
  return { ticket: result.ticket, generation: directory.generation, context: directory.context, target };
}

export async function executePortfolio(input: Command, pending: Pending, signal: AbortSignal) {
  const { target, ticket } = pending;
  const path = target === null ? directoryPath : directoryPath + '/' + target;
  return validResult(await read(path, signal, write(target === null ? 'POST' : 'PATCH', input, ticket), pending), pending);
}

export async function reconcilePortfolio(pending: Pending, close: boolean, signal: AbortSignal) {
  const path = ticketPath + '/' + pending.ticket;
  return validResult(await read(close ? path + '/close' : path, signal, close ? write('POST') : {}), pending);
}
