// Run the whole app locally: `npm start`
// Starts the FastAPI backend (port 8000) and the React dev server (port 5173) together.
import { existsSync } from 'node:fs';
import { spawn, spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { ROOT, VENV_PYTHON } from './common.mjs';

const VITE = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js');

if (!existsSync(VENV_PYTHON) || !existsSync(VITE)) {
  console.error('\n❌ Dependencies are not installed. Run `npm run setup` first.\n');
  process.exit(1);
}

const processes = [
  {
    name: 'api',
    color: '\x1b[36m',
    cmd: VENV_PYTHON,
    args: ['-m', 'uvicorn', 'api.index:app', '--reload', '--port', '8000'],
  },
  {
    name: 'web',
    color: '\x1b[35m',
    cmd: process.execPath,
    args: [VITE, '--open'],
  },
];

let stopping = false;
const children = processes.map(({ name, color, cmd, args }) => {
  const child = spawn(cmd, args, { cwd: ROOT, env: { ...process.env, FORCE_COLOR: '1' } });
  const prefix = `${color}[${name}]\x1b[0m `;
  const forward = (stream, target) => {
    let buffer = '';
    stream.on('data', (chunk) => {
      buffer += chunk;
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop();
      for (const line of lines) target.write(prefix + line + '\n');
    });
  };
  forward(child.stdout, process.stdout);
  forward(child.stderr, process.stderr);
  child.on('exit', (code) => {
    if (!stopping) {
      console.error(`${prefix}exited with code ${code}; stopping.`);
      stop(code ?? 1);
    }
  });
  return child;
});

function stop(code = 0) {
  stopping = true;
  for (const child of children) {
    if (child.exitCode !== null) continue;
    // uvicorn --reload runs the server in a child process; on Windows kill the whole tree.
    if (process.platform === 'win32') spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    else child.kill();
  }
  process.exit(code);
}

process.on('SIGINT', () => stop(0));
process.on('SIGTERM', () => stop(0));

console.log('\n▶ Ledger Reconciliation is starting');
console.log('  App: http://localhost:5173   (API: http://localhost:8000/api/health)');
console.log('  Press Ctrl+C to stop.\n');
