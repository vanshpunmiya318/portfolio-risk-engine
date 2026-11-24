// Windows integration checks: node --test scripts/dev.test.mjs
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { appendFileSync, cpSync, existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { createConnection, createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = fileURLToPath(new URL('../', import.meta.url));

async function listen() {
  const server = createServer(socket => socket.end());
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return server;
}

const close = server => new Promise(resolve => server.close(resolve));

async function canConnect(port) {
  return new Promise(resolve => {
    const socket = createConnection({ host: '127.0.0.1', port });
    const finish = connected => { socket.destroy(); resolve(connected); };
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
    socket.setTimeout(1000, () => finish(false));
  });
}

async function waitFor(predicate, run) {
  const deadline = Date.now() + 70000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, `Launcher wait timed out:\n${run.output}`);
    await delay(50);
  }
}

function databaseHashes(directory) {
  return readdirSync(directory).filter(name => name.endsWith('.db')).sort().map(name =>
    [name, createHash('sha256').update(readFileSync(join(directory, name))).digest('hex')]);
}

test('Windows dev.cmd database readiness and cleanup', { skip: process.platform !== 'win32' }, async t => {
  const project = mkdtempSync(join(tmpdir(), 'riskengine-dev-test-'));
  const active = new Set();
  t.after(async () => {
    for (const run of active) run.child.stdin.end('stop\n');
    await Promise.all([...active].map(run => waitFor(() => run.closed, run)));
    rmSync(project, { recursive: true, force: true });
  });
  const bundled = join(root, 'data');
  const bundledBefore = existsSync(bundled) ? databaseHashes(bundled) : [];
  mkdirSync(join(project, 'scripts'));
  mkdirSync(join(project, 'frontend'));
  for (const name of ['dev.cmd', 'scripts/dev.mjs', 'src', 'frontend/index.html',
    'frontend/src', 'frontend/package.json', 'frontend/vite.config.ts', 'frontend/node_modules']) {
    cpSync(join(root, name), join(project, name), {
      recursive: true,
      filter: path => !/[\\/](?:__pycache__|\.vite(?:-temp)?)$/.test(path),
    });
  }
  symlinkSync(join(root, '.venv'), join(project, '.venv'), 'junction');

  const healthy = join(project, 'healthy.db');
  const empty = join(project, 'empty.db');
  const missing = join(project, 'missing.db');
  const malformed = join(project, 'malformed.db');
  writeFileSync(malformed, 'not a SQLite database');
  const env = { ...process.env, PYTHONPATH: join(project, 'src'), RISKENGINE_DEMO: '0' };
  execFileSync(join(project, '.venv', 'Scripts', 'python.exe'), ['-B', '-c', [
    'import sqlite3, sys',
    'from contextlib import closing',
    'from riskengine.db.schema.loader import create_schema',
    'create_schema(sys.argv[1])',
    'create_schema(sys.argv[2])',
    'with closing(sqlite3.connect(sys.argv[1])) as connection, connection:',
    "    connection.execute(\"INSERT INTO portfolios VALUES (1, 'Test', 'USD')\")",
  ].join('\n'), healthy, empty], { cwd: project, env });
  const data = join(project, 'data');
  mkdirSync(data);
  for (const name of ['historical_portfolio.db', 'demo_portfolio.db']) {
    const source = join(bundled, name);
    cpSync(existsSync(source) ? source : healthy, join(data, name));
  }
  const dataBefore = databaseHashes(data);
  appendFileSync(join(project, 'src', 'riskengine', 'api', 'main.py'), `

if timeout_mode := os.environ.get("LAUNCHER_TEST_TIMEOUT"):
    import asyncio
    import uvicorn

    @asynccontextmanager
    async def test_lifespan(application):
        print(f"Timeout fixture backend PID {os.getpid()}, parent PID {os.getppid()}", flush=True)
        if timeout_mode == "no-listener":
            await asyncio.Event().wait()
        yield

    app = FastAPI(lifespan=test_lifespan)
    release_requests = asyncio.Event()

    if timeout_mode == "exits-during-cleanup":
        original_shutdown = uvicorn.Server.shutdown

        async def release_on_shutdown(server, *args, **kwargs):
            release_requests.set()
            await original_shutdown(server, *args, **kwargs)

        uvicorn.Server.shutdown = release_on_shutdown

    @app.get("/ready")
    async def stalled_readiness():
        await release_requests.wait()
        return {"status": "ready"}
`);
  const before = databaseHashes(project);
  const held = await listen();
  const spare = await listen();
  const apiPort = held.address().port;
  const vitePort = spare.address().port;
  await close(spare);
  const lock = join(project, 'tmp', 'portfolio-risk-dev.lock');

  function start(db, extraEnv = {}) {
    const child = spawn('cmd.exe', ['/d', '/c', 'dev.cmd'], {
      cwd: project, windowsHide: true, stdio: 'pipe',
      env: { ...env, RISKENGINE_DB: db, RISKENGINE_API_PORT: String(apiPort),
        RISKENGINE_VITE_PORT: String(vitePort), VITE_PORTFOLIO_ID: '999', ...extraEnv },
    });
    const run = { child, output: '', closed: false, code: null };
    active.add(run);
    for (const stream of [child.stdout, child.stderr]) stream.on('data', data => { run.output += data; });
    child.stdin.on('error', () => {});
    child.on('error', error => { run.output += error.message; });
    child.on('close', code => {
      Object.assign(run, { closed: true, code });
      active.delete(run);
    });
    return run;
  }

  async function stopped(run, code) {
    await waitFor(() => run.closed, run);
    assert.equal(run.code, code, run.output);
    assert.match(run.output, /Development servers stopped/);
    assert.equal(existsSync(lock), false);
    assert.equal(await canConnect(apiPort), false);
    assert.equal(await canConnect(vitePort), false);
  }

  try {
    await t.test('occupied port preserves the existing listener', async () => {
      const run = start(healthy);
      await waitFor(() => run.closed, run);
      assert.equal(run.code, 1, run.output);
      assert.match(run.output, new RegExp(`Port ${apiPort} is occupied`));
      assert.doesNotMatch(run.output, /Ready: http/);
      assert.equal(existsSync(lock), false);
      assert.equal(await canConnect(apiPort), true);
      assert.equal(await canConnect(vitePort), false);
    });
  } finally {
    await close(held);
  }

  for (const [label, file] of [['missing', missing], ['malformed', malformed]]) {
    await t.test(`${label} database fails and cleans up`, async () => {
      const run = start(file);
      await stopped(run, 1);
      assert.match(run.output, /Database is not ready \(\/ready returned HTTP 503\)/);
      assert.match(run.output, /Check that RISKENGINE_DB.*RISKENGINE_DEMO=1.*\/ready/);
      assert.doesNotMatch(run.output, /Ready: http/);
      assert.match(run.output, /Finished server process/);
    });
  }

  async function startsAndStops(file, extraEnv, expectedMode) {
    const run = start(file, extraEnv);
    await waitFor(() => run.output.includes('Ready: http') || run.closed, run);
    assert.match(run.output, /Ready: http/, run.output);
    if (expectedMode) assert.match(run.output, new RegExp(`mode=${expectedMode} access=read-only`));
    assert.equal(existsSync(lock), true);
    for (const [port, route] of [[apiPort, '/ready'], [vitePort, '/holdings']]) {
      const response = await fetch(`http://127.0.0.1:${port}${route}`, { signal: AbortSignal.timeout(3000) });
      assert.equal(response.status, 200);
      await response.arrayBuffer();
    }
    run.child.stdin.end('stop\n');
    await stopped(run, 0);
  }

  await t.test('healthy historical database starts and shuts down cleanly', async () => {
    await startsAndStops(undefined, {}, 'historical');
  });
  await t.test('demo database takes precedence over a missing override', async () => {
    await startsAndStops(missing, { RISKENGINE_DEMO: '1' }, 'demo');
  });
  await t.test('valid empty database starts and shuts down cleanly', async () => {
    await startsAndStops(empty);
  });

  for (const mode of ['no-listener', 'stalled', 'exits-during-cleanup']) {
    await t.test(`${mode}: timeout terminates owned processes and permits same-port restart`, async () => {
      const run = start(healthy, { LAUNCHER_TEST_TIMEOUT: mode });
      await waitFor(() => run.output.includes('Timeout fixture backend PID') || run.closed, run);
      const pids = run.output.match(/Timeout fixture backend PID (\d+), parent PID (\d+)/);
      assert.ok(pids, run.output);
      const began = Date.now();
      await waitFor(() => run.output.includes('could not be confirmed within 30 seconds') || run.closed, run);
      assert.ok(Date.now() - began < 32000, run.output);
      assert.doesNotMatch(run.output, /Ready: http/);
      await waitFor(() => run.closed, run);
      assert.match(run.output, /could not be confirmed within 30 seconds/);
      for (const pid of pids.slice(1)) {
        assert.throws(() => process.kill(Number(pid), 0), { code: 'ESRCH' }, run.output);
      }
      await stopped(run, 1);
      if (mode === 'exits-during-cleanup') assert.match(run.output, /Finished server process/);
      await startsAndStops(healthy);
    });
  }

  assert.equal(existsSync(missing), false);
  assert.deepEqual(databaseHashes(project), before);
  assert.deepEqual(databaseHashes(data), dataBefore);
  assert.deepEqual(existsSync(bundled) ? databaseHashes(bundled) : [], bundledBefore);
});
