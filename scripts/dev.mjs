// Windows dev supervisor: existing virtualenv + npm-managed Vite, no new dependencies.
import { spawn, fork } from 'node:child_process';
import { existsSync, mkdirSync, openSync, closeSync, writeFileSync, readFileSync, unlinkSync } from 'node:fs';
import { createServer as createTcpServer, createConnection } from 'node:net';
import { createInterface } from 'node:readline';
import { randomBytes, randomUUID } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const script = fileURLToPath(import.meta.url);
const root = dirname(dirname(script));
const frontend = join(root, 'frontend');
const apiPort = Number(process.env.RISKENGINE_API_PORT ?? '8000');
const vitePort = Number(process.env.RISKENGINE_VITE_PORT ?? '5173');
const browserConfirmationMs = 30_000;

if (process.argv.includes('--vite-child')) {
  let server;
  let stopping = false;
  async function stop() {
    if (stopping) return;
    stopping = true;
    if (server) await server.close();
    process.exit(0);
  }
  process.on('message', (message) => { if (message?.type === 'stop') void stop(); });
  process.on('disconnect', () => void stop());
  process.on('SIGINT', () => void stop());
  process.on('SIGTERM', () => void stop());
  try {
    const { createServer } = await import(pathToFileURL(join(frontend, 'node_modules/vite/dist/node/index.js')).href);
    const browserHost = `127.0.0.1:${vitePort}`;
    server = await createServer({ root: frontend, configFile: join(frontend, 'vite.config.ts'),
      plugins: [{
        name: 'riskengine-local-host',
        configureServer(vite) {
          vite.middlewares.use((request, response, next) => {
            const hosts = request.headersDistinct.host;
            if (hosts?.length !== 1 || hosts[0] !== browserHost
                || 'forwarded' in request.headers || 'x-forwarded-host' in request.headers) {
              response.writeHead(400, { 'Cache-Control': 'no-store' }).end('Invalid local request host.');
              return;
            }
            next();
          });
        },
      }],
      server: { host: '127.0.0.1', port: vitePort, strictPort: true,
        cors: false, fs: { allow: [frontend] },
        headers: { 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
          'Content-Security-Policy': "frame-ancestors 'none'" },
        proxy: { '/api': { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false } } } });
    await server.listen();
    process.send?.({ type: 'ready' });
  } catch (error) {
    console.error(`Vite startup failed: ${error.message}`);
    if (server) await server.close();
    process.exit(1);
  }
} else {
  await run();
}

async function portAvailable(port) {
  // Windows can allow a wildcard bind beside an existing specific-address
  // listener. Check actual connectivity before probing the exact bind address.
  await new Promise((resolve, reject) => {
    const socket = createConnection({ host: '127.0.0.1', port });
    socket.once('connect', () => {
      socket.destroy();
      reject(new Error(`Port ${port} is occupied. Stop its existing server or explicitly configure different dev ports.`));
    });
    socket.once('error', (error) => {
      if (error.code === 'ECONNREFUSED') resolve();
      else reject(new Error(`Cannot verify port ${port}: ${error.message}`));
    });
    socket.setTimeout(1000, () => { socket.destroy(); reject(new Error(`Port ${port} check timed out.`)); });
  });
  await new Promise((resolve, reject) => {
    const probe = createTcpServer();
    probe.once('error', () => reject(new Error(`Port ${port} is occupied. Stop its existing server before running dev.cmd.`)));
    probe.listen({ host: '127.0.0.1', port, exclusive: true }, () => probe.close(resolve));
  });
}

function waitForExit(child, timeout) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
  if (!child.pid) return Promise.resolve(false);
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.off('exit', exited);
      resolve(false);
    }, timeout);
    function exited() {
      clearTimeout(timer);
      resolve(true);
    }
    child.once('exit', exited);
  });
}

async function run() {
  const python = join(root, '.venv', 'Scripts', 'python.exe');
  const lock = join(root, 'tmp', 'portfolio-risk-dev.lock');
  const children = [];
  let lockOwned = false;
  let stopping = false;
  let shutdown;
  let input;
  let readinessServer;
  let apiControlSocket;

  async function stop(code) {
    if (stopping) return shutdown;
    stopping = true;
    shutdown = (async () => {
      let cleanupFailed = false;
      input?.close();
      apiControlSocket?.end('stop\n');
      if (readinessServer?.listening) readinessServer.close();
      for (const child of children) {
        if (child.connected) child.send({ type: 'stop' }, () => {});
        else if (child.stdin?.writable) child.stdin.end('stop\n');
      }
      await Promise.all(children.map(async (child) => {
        if (!child.pid || await waitForExit(child, 5000)) return;
        if (child.exitCode !== null || child.signalCode !== null) return;
        // Run against the live child we spawned, never a PID read from a lock or port.
        const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
        killer.on('error', () => {});
        const killCompleted = await waitForExit(killer, 5000);
        if (!killCompleted) killer.kill();
        const exited = await waitForExit(child, 2000);
        // After a failed tree kill, the root exiting alone cannot confirm descendant termination.
        if (!killCompleted || killer.exitCode !== 0 || !exited) {
          cleanupFailed = true;
          console.error(`Could not confirm termination of owned process tree ${child.pid}.`);
        }
      }));
      if (lockOwned && !cleanupFailed) {
        try {
          if (readFileSync(lock, 'utf8') !== String(process.pid)) throw new Error('Lock ownership changed.');
          unlinkSync(lock);
        } catch (error) {
          cleanupFailed = true;
          console.error(`Could not clear dev lock: ${error.message}`);
        }
      }
      if (cleanupFailed) console.error('Before removing tmp\\portfolio-risk-dev.lock, confirm that the launcher and its backend and frontend processes have exited and the configured ports are free.');
      console.log(cleanupFailed ? 'Development shutdown needs manual inspection.' : 'Development servers stopped.');
      process.exit(cleanupFailed ? 1 : code);
    })();
    return shutdown;
  }

  function watch(child, label) {
    children.push(child);
    child.stdin?.on('error', () => {});
    child.once('error', (error) => {
      console.error(`${label} failed: ${error.message}`);
      void stop(1);
    });
    child.once('exit', (code, signal) => {
      if (!stopping) {
        console.error(`${label} exited unexpectedly (${signal ?? code}). Stopping both servers.`);
        void stop(1);
      }
    });
    return child;
  }

  process.on('SIGINT', () => void stop(0));
  process.on('SIGTERM', () => void stop(0));
  for (const event of ['uncaughtException', 'unhandledRejection']) {
    process.on(event, (error) => { console.error(error); void stop(1); });
  }
  try {
    if (process.platform !== 'win32') throw new Error('This launcher uses the Windows .venv/Scripts/python.exe environment.');
    if (![apiPort, vitePort].every((port) => Number.isInteger(port) && port >= 1024 && port <= 65535) || apiPort === vitePort) {
      throw new Error('Dev ports must be different integer values between 1024 and 65535.');
    }
    if (!existsSync(python)) throw new Error('Missing .venv\\Scripts\\python.exe. Use the existing project virtual environment.');
    if (!existsSync(join(frontend, 'node_modules/vite/dist/node/index.js'))) throw new Error('Frontend dependencies are missing. Run npm.cmd ci inside frontend first.');
    mkdirSync(dirname(lock), { recursive: true });
    let handle;
    try { handle = openSync(lock, 'wx'); }
    catch { throw new Error('A dev runner lock exists at tmp\\portfolio-risk-dev.lock. Another runner may be active. Before removing the lock, confirm the prior launcher and its backend and frontend processes have exited and both configured ports are free.'); }
    lockOwned = true;
    try { writeFileSync(handle, String(process.pid)); }
    finally { closeSync(handle); }
    input = createInterface({ input: process.stdin });
    input.on('line', (line) => { if (line.trim().toLowerCase() === 'stop') void stop(0); });
    input.on('close', () => { if (!stopping) void stop(0); });
    await portAvailable(apiPort);
    await portAvailable(vitePort);

    // A private startup handshake identifies our API process without trusting
    // an unrelated health response or buffered Windows launcher stdout.
    let apiReady = false;
    let browserConfirmedAt = Infinity;
    const token = randomUUID();
    readinessServer = createTcpServer((socket) => {
      let message = '';
      socket.setTimeout(1000, () => socket.destroy());
      socket.on('data', (chunk) => {
        message += chunk.toString();
        if (message.length > 128) return socket.destroy();
        for (let newline = message.indexOf('\n'); newline !== -1; newline = message.indexOf('\n')) {
          const line = message.slice(0, newline);
          message = message.slice(newline + 1);
          if (!apiControlSocket && line === token) {
            apiReady = true;
            apiControlSocket = socket;
            socket.setTimeout(0);
            readinessServer.close();
          } else if (socket === apiControlSocket && line.startsWith('browser-ready:')) {
            if (line === `browser-ready:${token}`) browserConfirmedAt = Math.min(browserConfirmedAt, performance.now());
          } else {
            return socket.destroy();
          }
        }
      });
      socket.on('error', () => socket.destroy());
    });
    await new Promise((resolve, reject) => {
      readinessServer.once('error', reject);
      readinessServer.listen(0, '127.0.0.1', resolve);
    });
    const readinessPort = readinessServer.address().port;

    // Async control avoids a blocking Windows stdin reader. EOF on this
    // private connection also shuts Uvicorn down if the supervisor disappears.
    const pythonCode = [
      'import asyncio, uvicorn',
      'class DevServer(uvicorn.Server):',
      '    async def watch_control(self, reader, writer):',
      '        try:',
      '            await reader.readline()',
      '        finally:',
      '            self.should_exit = True',
      '            writer.close()',
      '    async def notify_browser(self, writer):',
      '        from riskengine.api.main import app',
      '        access = getattr(app.state, "local_access", None)',
      '        if access:',
      '            await access.browser_confirmed.wait()',
      '            if not writer.is_closing():',
      '                try:',
      `                    writer.write((${JSON.stringify(`browser-ready:${token}`)} + "\\n").encode())`,
      '                    await writer.drain()',
      '                except ConnectionError:',
      '                    pass',
      '    async def startup(self, sockets=None):',
      '        await super().startup(sockets)',
      '        if self.started:',
      `            reader, writer = await asyncio.open_connection("127.0.0.1", ${readinessPort})`,
      `            writer.write((${JSON.stringify(token)} + "\\n").encode())`,
      '            await writer.drain()',
      '            self.control_task = asyncio.create_task(self.watch_control(reader, writer))',
      '            self.browser_task = asyncio.create_task(self.notify_browser(writer))',
      `server = DevServer(uvicorn.Config("riskengine.api.main:app", host="127.0.0.1", port=${apiPort}, proxy_headers=False, access_log=False))`,
      'server.run()',
    ].join('\n');
    const bootstrap = randomBytes(32).toString('base64url');
    const api = watch(spawn(python, ['-B', '-u', '-c', pythonCode], {
      cwd: root, windowsHide: true, stdio: ['ignore', 'inherit', 'inherit'],
      env: { ...process.env, RISKENGINE_API_MODE: 'protected', RISKENGINE_API_PORT: String(apiPort),
        RISKENGINE_VITE_PORT: String(vitePort), RISKENGINE_BOOTSTRAP_CODE: bootstrap },
    }), 'FastAPI');
    const deadline = Date.now() + 30000;
    const readinessUrl = `http://127.0.0.1:${apiPort}/ready`;
    const databaseHelp = `Check that RISKENGINE_DB points to a usable SQLite database. RISKENGINE_DEMO=1 overrides it and selects the demo database. With neither set, provision data/historical_portfolio.db. Check ${readinessUrl} while FastAPI is running.`;
    let ready = false;
    while (!stopping && api.exitCode === null && api.signalCode === null) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      if (apiReady) {
        const response = await fetch(readinessUrl, {
          redirect: 'manual', signal: AbortSignal.timeout(Math.min(500, remaining)),
        }).catch(() => undefined); // Retry transport failures within the startup deadline.
        if (response) {
          await response.body?.cancel();
          if (stopping || api.exitCode !== null || api.signalCode !== null) return;
          if (response.status !== 200) {
            throw new Error(`Database is not ready (/ready returned HTTP ${response.status}). ${databaseHelp}`);
          }
          ready = true;
          break;
        }
      }
      await new Promise((resolve) => setTimeout(resolve, Math.min(150, Math.max(0, deadline - Date.now()))));
    }
    if (stopping || api.exitCode !== null || api.signalCode !== null) return;
    if (!ready) throw new Error(`Database readiness could not be confirmed within 30 seconds. ${databaseHelp}`);
    const vite = watch(fork(script, ['--vite-child'], {
      cwd: root, windowsHide: true, stdio: ['pipe', 'inherit', 'inherit', 'ipc'],
    }), 'Vite');
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Vite did not become ready within 30 seconds.')), 30000);
      vite.on('message', (message) => { if (message?.type === 'ready') { clearTimeout(timer); resolve(); } });
      vite.once('exit', () => { clearTimeout(timer); reject(new Error('Vite failed during startup.')); });
    });
    if (stopping) return;
    const origin = `http://127.0.0.1:${vitePort}`;
    const page = await fetch(`${origin}/holdings`, { redirect: 'manual', signal: AbortSignal.timeout(3000) });
    await page.body?.cancel();
    if (page.status !== 200 || !page.headers.get('content-type')?.startsWith('text/html')) {
      throw new Error('The local browser application could not be loaded.');
    }
    const probe = await fetch(`${origin}/api/local-session`, {
      method: 'HEAD', headers: { Origin: origin, 'X-RiskEngine-Bootstrap': bootstrap },
      redirect: 'manual', signal: AbortSignal.timeout(3000),
    });
    if (stopping) return;
    if (probe.status !== 204) throw new Error('The protected browser session could not be initialized.');
    const browserDeadline = performance.now() + browserConfirmationMs;
    await new Promise((resolve, reject) => {
      // Only the short-lived, single-use code is carried in navigation state.
      const browser = spawn('rundll32.exe', ['url.dll,FileProtocolHandler', `${origin}/holdings#launch=${bootstrap}`], {
        windowsHide: true, stdio: 'ignore', detached: true,
      });
      const dispatchFailure = () => reject(new Error('Could not open the browser. Restart dev.cmd and check the Windows default browser.'));
      const timer = setTimeout(() => {
        browser.kill();
        dispatchFailure();
      }, 5000);
      browser.once('error', () => {
        clearTimeout(timer);
        dispatchFailure();
      });
      browser.once('exit', code => {
        clearTimeout(timer);
        if (code === 0) resolve();
        else dispatchFailure();
      });
    });
    while (!stopping && browserConfirmedAt > browserDeadline && performance.now() < browserDeadline) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (stopping) return;
    if (browserConfirmedAt > browserDeadline) {
      throw new Error('Browser authorization was not confirmed within 30 seconds. Check that the browser opened, allows cookies for this local site, and completed session initialization. Restart dev.cmd and use the tab it opens.');
    }
    console.log(`\nReady: http://127.0.0.1:${vitePort}/holdings\nFastAPI: http://127.0.0.1:${apiPort}\nPress Ctrl+C or type stop then Enter to shut down both servers.`);
  } catch (error) {
    if (stopping) return;
    console.error(`Development startup failed: ${error.message}`);
    await stop(1);
  }
}
