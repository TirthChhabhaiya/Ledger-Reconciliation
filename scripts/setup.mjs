// One-time setup after cloning: `npm run setup`
// Creates .venv, installs the Python requirements into it, and installs the npm packages.
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { ROOT, VENV_DIR, VENV_PYTHON, run } from './common.mjs';

// The pinned pandas / numpy versions publish Windows wheels for Python 3.10–3.13 only;
// newer Pythons would try to compile them from source and fail.
const SUPPORTED_MINORS = [12, 13, 11, 10];
const INSTALL_HINT =
  '   Install Python 3.12 from https://www.python.org/downloads/release/python-3128/\n' +
  '   (it can sit alongside other Python versions), then run `npm run setup` again.\n' +
  '   (Or set the PYTHON environment variable to a Python 3.12 executable.)\n';

function pythonVersion(cmd, args = []) {
  const result = spawnSync(cmd, [...args, '--version'], { encoding: 'utf8' });
  const match = /Python (\d+)\.(\d+)/.exec(`${result.stdout}${result.stderr}`);
  return result.status === 0 && match ? { major: Number(match[1]), minor: Number(match[2]) } : null;
}

const isSupported = (version) => version?.major === 3 && SUPPORTED_MINORS.includes(version.minor);

function findPython() {
  const candidates = [
    ...(process.env.PYTHON ? [[process.env.PYTHON]] : []),
    ...(process.platform === 'win32' ? SUPPORTED_MINORS.map((minor) => ['py', `-3.${minor}`]) : []),
    ...SUPPORTED_MINORS.map((minor) => [`python3.${minor}`]),
    ['python3'],
    ['python'],
  ];
  const unsupported = new Set();
  for (const [cmd, ...args] of candidates) {
    const version = pythonVersion(cmd, args);
    if (!version) continue;
    if (isSupported(version)) return { cmd, args, version: `3.${version.minor}` };
    unsupported.add(`${version.major}.${version.minor}`);
  }
  return { unsupported: [...unsupported] };
}

if (existsSync(VENV_PYTHON)) {
  const version = pythonVersion(VENV_PYTHON);
  if (!isSupported(version)) {
    console.error(
      `\n❌ The existing .venv uses Python ${version ? `${version.major}.${version.minor}` : '(unknown)'}, ` +
        'which this project does not support.\n   Delete the .venv folder and run `npm run setup` again.\n',
    );
    process.exit(1);
  }
  console.log(`\n▶ Using existing virtual environment (${VENV_DIR})`);
} else {
  const python = findPython();
  if (!python.cmd) {
    const found = python.unsupported.length
      ? `   Found Python ${python.unsupported.join(', ')}, which this project's packages do not support yet.\n`
      : '';
    console.error('\n❌ Python 3.10 – 3.13 was not found.\n' + found + INSTALL_HINT);
    process.exit(1);
  }
  console.log(`\n▶ Creating virtual environment with Python ${python.version} (${VENV_DIR})`);
  run(python.cmd, [...python.args, '-m', 'venv', VENV_DIR]);
}

console.log('\n▶ Installing Python packages (requirements.txt)');
run(VENV_PYTHON, ['-m', 'pip', 'install', '--upgrade', 'pip']);
run(VENV_PYTHON, ['-m', 'pip', 'install', '-r', 'requirements.txt']);

console.log('\n▶ Installing npm packages');
run('npm', ['install'], { shell: true });

console.log(`\n✅ Setup complete in ${ROOT}\n   Start the app with: npm start\n`);
