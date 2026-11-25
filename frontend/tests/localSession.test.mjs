import assert from 'node:assert/strict';
import test from 'node:test';
import { apiFetch, checkLocalSession, initializeLocalSession, onSessionLost } from '../src/api/localSession.ts';

test('bootstrap removes the fragment before exchange and builds on the HttpOnly cookie', async t => {
  const code = 'a'.repeat(43);
  const calls = [];
  const location = { hash: '#launch=' + code, pathname: '/holdings', search: '?view=details' };
  const history = { state: null, replaceState: (_, __, address) => calls.push(address) };
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    calls.push([path, options]);
    return options.method === 'POST' ? new Response(null, { status: 204 })
      : Response.json({ generation: 'current-instance' });
  });
  assert.equal(await initializeLocalSession(location, history), 'current-instance');
  assert.equal(calls[0], '/holdings?view=details');
  assert.equal(calls[1][0], '/api/local-session');
  assert.deepEqual(calls[1][1].headers, { 'X-RiskEngine-Bootstrap': code });
  assert.equal(calls[1][1].credentials, 'same-origin');
  assert.equal(calls[2][1].headers, undefined);
});

test('refresh and direct navigation only check the existing session', async t => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ generation: 'active' }));
  assert.equal(await initializeLocalSession({ hash: '#main-content' }, {}), 'active');
  assert.equal(fetch.mock.callCount(), 1);
  assert.equal(fetch.mock.calls[0].arguments[1].method, undefined);
});

test('an unauthorized response notifies the session gate without retry', async t => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 401 }));
  let losses = 0;
  const unsubscribe = onSessionLost(() => losses++);
  try {
    assert.equal((await apiFetch('/api/portfolios/1/holdings')).status, 401);
    await assert.rejects(checkLocalSession(), /unavailable/);
    assert.equal(fetch.mock.callCount(), 2);
    assert.equal(losses, 2);
  } finally {
    unsubscribe();
  }
});

test('concurrent 401s close the boundary once and suppress older financial and session successes', async t => {
  const session = await import('../src/api/localSession.ts?concurrent-loss');
  session.protectLocalSessionRequests();
  const pending = [];
  const fetch = t.mock.method(globalThis, 'fetch', (_path, options) =>
    new Promise(resolve => pending.push({ resolve, signal: options.signal })));
  let losses = 0;
  session.onSessionLost(() => losses++);

  const oldCheck = session.checkLocalSession();
  const oldFinancial = session.apiFetch('/api/portfolios/1/holdings');
  const oldCheckRejected = assert.rejects(oldCheck, { name: 'AbortError' });
  const oldFinancialRejected = assert.rejects(oldFinancial, { name: 'AbortError' });
  const first = session.apiFetch('/api/portfolio/summary');
  const second = session.apiFetch('/api/portfolios/1/performance');
  pending[2].resolve(new Response(null, { status: 401 }));
  pending[3].resolve(new Response(null, { status: 401 }));
  await Promise.all([first, second]);
  assert.equal(losses, 1);
  assert.ok(pending.every(request => request.signal.aborted));
  pending[0].resolve(Response.json({ generation: 'earlier' }));
  pending[1].resolve(Response.json({ private: 'old data' }));
  await Promise.all([oldCheckRejected, oldFinancialRejected]);
  await assert.rejects(session.checkLocalSession(), { name: 'AbortError' });
  assert.equal(fetch.mock.callCount(), 4);
});

test('a changed backend generation invalidates an in-flight financial request', async t => {
  const session = await import('../src/api/localSession.ts?changed-generation');
  session.protectLocalSessionRequests();
  let finish;
  let signal;
  t.mock.method(globalThis, 'fetch', (path, options) => {
    if (path === '/api/local-session') return Promise.resolve(Response.json({ generation: 'new' }));
    signal = options.signal;
    return new Promise(resolve => { finish = resolve; });
  });
  const financial = session.apiFetch('/api/portfolios/1/report');
  const rejected = assert.rejects(financial, { name: 'AbortError' });
  await session.verifyLocalSession('previous');
  assert.equal(signal.aborted, true);
  finish(Response.json({ private: 'old report' }));
  await rejected;
});
