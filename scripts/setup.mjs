// One-time setup after cloning: `npm run setup`
// Creates .venv, installs the Python requirements into it, and installs the npm packages.
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { ROOT, VENV_DIR, VENV_PYTHON, run } from './common.mjs';

const MIN_PYTHON = [3, 10];

function findPython() {
  const candidates = [
    ...(process.env.PYTHON ? [[process.env.PYTHON]] : []),
    ...(process.platform === 'win32' ? [['py', '-3.12'], ['py', '-3']] : []),
    ['python3'],
    ['python'],
  ];
  for (const [cmd, ...args] of candidates) {
    const result = spawnSync(cmd, [...args, '--version'], { encoding: 'utf8' });
    const match = /Python (\d+)\.(\d+)/.exec(`${result.stdout}${result.stderr}`);
    if (result.status === 0 && match) {
      const [major, minor] = [Number(match[1]), Number(match[2])];
      if (major > MIN_PYTHON[0] || (major === MIN_PYTHON[0] && minor >= MIN_PYTHON[1])) {
        return { cmd, args, version: `${major}.${minor}` };
      }
    }
  }
  return null;
}

if (!existsSync(VENV_PYTHON)) {
  const python = findPython();
  if (!python) {
    console.error(
      `\n❌ Python ${MIN_PYTHON.join('.')}+ was not found.\n` +
        '   Install Python 3.12 from https://www.python.org/downloads/ and tick\n' +
        '   "Add python.exe to PATH" during install, then run `npm run setup` again.\n' +
        '   (Or set the PYTHON environment variable to your python executable.)\n',
    );
    process.exit(1);
  }
  console.log(`\n▶ Creating virtual environment with Python ${python.version} (${VENV_DIR})`);
  run(python.cmd, [...python.args, '-m', 'venv', VENV_DIR]);
} else {
  console.log(`\n▶ Using existing virtual environment (${VENV_DIR})`);
}

console.log('\n▶ Installing Python packages (requirements.txt)');
run(VENV_PYTHON, ['-m', 'pip', 'install', '--upgrade', 'pip']);
run(VENV_PYTHON, ['-m', 'pip', 'install', '-r', 'requirements.txt']);

console.log('\n▶ Installing npm packages');
run('npm', ['install'], { shell: true });

console.log(`\n✅ Setup complete in ${ROOT}\n   Start the app with: npm start\n`);
