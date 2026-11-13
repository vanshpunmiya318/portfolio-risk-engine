// Windows dev supervisor: existing virtualenv + npm-managed Vite, no new dependencies.
import { spawn, fork } from 'node:child_process';
import { existsSync, mkdirSync, openSync, closeSync, writeFileSync, readFileSync, unlinkSync } from 'node:fs';
import { createServer as createTcpServer, createConnection } from 'node:net';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const script = fileURLToPath(import.meta.url);
const root = dirname(dirname(script));
const frontend = join(root, 'frontend');
const apiPort = Number(process.env.RISKENGINE_API_PORT ?? '8000');
const vitePort = Number(process.env.RISKENGINE_VITE_PORT ?? '5173');

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
    server = await createServer({ root: frontend, configFile: join(frontend, 'vite.config.ts'),
      server: { host: '127.0.0.1', port: vitePort, strictPort: true,
        proxy: { '/api': { target: `http://127.0.0.1:${apiPort}`, changeOrigin: true } } } });
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
        if (child.exitCode !== null || child.signalCode !== null || !child.pid) return;
        await new Promise((resolve) => {
          const timer = setTimeout(resolve, 5000);
          child.once('exit', () => { clearTimeout(timer); resolve(); });
        });
        if (child.exitCode === null && child.signalCode === null) {
          // Fallback targets only our owned process tree, never a port owner.
          const result = await new Promise((resolve) => {
            const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
            killer.once('exit', resolve);
            killer.once('error', () => resolve(-1));
          });
          if (result !== 0 && child.exitCode === null && child.signalCode === null) {
            cleanupFailed = true;
            console.error(`Could not terminate owned process ${child.pid}; retaining the dev lock for inspection.`);
          }
        }
      }));
      if (lockOwned && !cleanupFailed) {
        try {
          if (readFileSync(lock, 'utf8') === String(process.pid)) unlinkSync(lock);
        } catch (error) { console.error(`Could not clear dev lock: ${error.message}`); }
      }
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
    catch { throw new Error('A dev runner lock exists at tmp\\portfolio-risk-dev.lock. Another runner may be active. After an abnormal shutdown, verify the prior runner is gone and its configured ports are unused before removing that lock.'); }
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
    const token = randomUUID();
    readinessServer = createTcpServer((socket) => {
      let message = '';
      socket.setTimeout(1000, () => socket.destroy());
      socket.on('data', (chunk) => {
        message += chunk.toString();
        if (message === token + '\n') {
          apiReady = true;
          apiControlSocket = socket;
          socket.setTimeout(0);
          readinessServer.close();
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
      '    async def startup(self, sockets=None):',
      '        await super().startup(sockets)',
      '        if self.started:',
      `            reader, writer = await asyncio.open_connection("127.0.0.1", ${readinessPort})`,
      `            writer.write((${JSON.stringify(token)} + "\\n").encode())`,
      '            await writer.drain()',
      '            self.control_task = asyncio.create_task(self.watch_control(reader, writer))',
      `server = DevServer(uvicorn.Config("riskengine.api.main:app", host="127.0.0.1", port=${apiPort}))`,
      'server.run()',
    ].join('\n');
    const api = watch(spawn(python, ['-B', '-u', '-c', pythonCode], {
      cwd: root, windowsHide: true, stdio: ['ignore', 'inherit', 'inherit'],
    }), 'FastAPI');
    const deadline = Date.now() + 30000;
    let ready = false;
    while (!stopping && Date.now() < deadline) {
      try {
        if (!apiReady) throw new Error('Waiting for the owned API process.');
        const response = await fetch(`http://127.0.0.1:${apiPort}/health`, { signal: AbortSignal.timeout(500) });
        if (response.ok && api.exitCode === null) { ready = true; break; }
      } catch { /* Retry while the owned API starts. */ }
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
    if (stopping) return;
    if (!ready) throw new Error('FastAPI did not become ready within 30 seconds.');
    const vite = watch(fork(script, ['--vite-child'], {
      cwd: root, windowsHide: true, stdio: ['pipe', 'inherit', 'inherit', 'ipc'],
    }), 'Vite');
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Vite did not become ready within 30 seconds.')), 30000);
      vite.on('message', (message) => { if (message?.type === 'ready') { clearTimeout(timer); resolve(); } });
      vite.once('exit', () => { clearTimeout(timer); reject(new Error('Vite failed during startup.')); });
    });
    if (stopping) return;
    console.log(`\nReady: http://127.0.0.1:${vitePort}/holdings\nFastAPI: http://127.0.0.1:${apiPort}\nPress Ctrl+C or type stop then Enter to shut down both servers.`);
  } catch (error) {
    console.error(`Development startup failed: ${error.message}`);
    await stop(1);
  }
}
