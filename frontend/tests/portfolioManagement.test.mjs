import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

let vite, api, provider;
const row = { id: 1, name: 'Portfolio', currency: 'USD' };
const second = { ...row, id: 2 };
const directory = {
  portfolios: [row, second], context: 'context', generation: 'generation',
  unsupported: [{ id: '9007199254740993', reason: 'outside_client_range' }],
  management_configured: true, write_availability: 'available', supported_currencies: ['USD', 'EUR'],
};
const ticket = 'a'.repeat(43);
const pending = { ticket, context: directory.context, generation: directory.generation, target: null };
const signal = () => new AbortController().signal;

before(async () => {
  vite = await createServer({ configFile: false, plugins: [react()],
    server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  api = await vite.ssrLoadModule('/src/api/portfolioManagement.ts');
  provider = await vite.ssrLoadModule('/src/PortfolioProvider.tsx');
});
after(async () => vite?.close());

test('explicit selection precedes ID 1, invalid configuration waits, and a sole supported portfolio is selected', () => {
  assert.deepEqual(provider.initialPortfolio([row, second], '2'), { id: 2, invalidConfiguration: false });
  for (const id of ['99', '0', '9007199254740993', 'garbage']) {
    assert.deepEqual(provider.initialPortfolio([row, second], id), { id: null, invalidConfiguration: true });
  }
  assert.equal(provider.initialPortfolio([row, second], undefined).id, 1);
  assert.equal(provider.initialPortfolio([second], undefined).id, 2);
  assert.equal(provider.initialPortfolio([second, { ...row, id: 3 }], undefined).id, null);
  assert.equal(provider.initialPortfolio([], undefined).id, null);
});

test('pending marker is opaque, survives a reload, and cannot be cleared by a different completion', () => {
  const items = new Map();
  const storage = { getItem: key => items.get(key) ?? null,
    setItem: (key, value) => items.set(key, value), removeItem: key => items.delete(key) };
  api.storePending(storage, pending);
  assert.deepEqual(api.readPending(storage), pending);
  assert.equal(api.clearPending(storage, 'b'.repeat(43)), false);
  assert.throws(() => api.storePending(storage, { ...pending, ticket: 'b'.repeat(43) }), /already pending/);
  assert.equal(api.clearPending(storage, ticket), true);
  assert.equal(api.readPending(storage), null);
  assert.throws(() => api.storePending({ ...storage, setItem() { throw new Error('unavailable'); } }, pending), /unavailable/);
});

test('directory preserves exact unsupported IDs and rejects unsafe selectable IDs', async t => {
  t.mock.method(globalThis, 'fetch', async () => Response.json(directory));
  assert.deepEqual(await api.loadPortfolios(signal()), directory);
  globalThis.fetch.mock.mockImplementation(async () => Response.json({ ...directory, portfolios: [{ ...row, id: 2 ** 53 }] }));
  await assert.rejects(() => api.loadPortfolios(signal()), /invalid/);
});

test('malformed and non-JSON management responses never expose parser details or payload fragments', async t => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({}));
  const input = { name: 'Example', currency: 'USD' };
  for (const response of [
    () => new Response('<html>private fragment</html>', { status: 502 }),
    () => new Response('{"private fragment"', { headers: { 'Content-Type': 'application/json' } }),
    () => Response.json({ unexpected: 'private fragment' }),
  ]) {
    fetch.mock.mockImplementation(async () => response());
    await assert.rejects(() => api.loadPortfolios(signal()), { message: 'The portfolio list response was invalid.' });
    await assert.rejects(() => api.preparePortfolio(input, null, directory, signal()), {
      message: 'The portfolio change could not be prepared. The server response was invalid.',
    });
    await assert.rejects(() => api.executePortfolio(input, pending, signal()), { message: 'The portfolio change response was invalid.' });
  }
});

test('ticket calls send exact proofs, never retry unrecognized failures, and validate outcome identity', async t => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    requests.push([path, options]);
    if (requests.length === 1) return Response.json({ ticket, outcome: 'prepared', reason: null, portfolio: null });
    if (requests.length === 2) return Response.json({ detail: 'Upstream error' }, { status: 502 });
    return Response.json({ ticket: 'b'.repeat(43), outcome: 'not_started', reason: null, portfolio: null });
  });
  const input = { name: 'Example', currency: 'USD' };
  assert.deepEqual(await api.preparePortfolio(input, null, directory, signal()), pending);
  await assert.rejects(() => api.executePortfolio(input, pending, signal()), /Upstream error/);
  assert.equal(requests.length, 2);
  assert.equal(requests[1][1].headers.get('X-RiskEngine-Ticket'), ticket);
  assert.equal(requests[1][1].headers.get('X-RiskEngine-Request'), '1');
  await assert.rejects(() => api.reconcilePortfolio(pending, true, signal()), /invalid/);
  assert.equal(requests[2][0], '/api/portfolios/mutations/' + ticket + '/close');
  assert.equal(requests[2][1].method, 'POST');
});
