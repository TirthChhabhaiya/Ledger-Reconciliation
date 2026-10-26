// One-time setup after cloning: `npm run setup`
// Creates .venv with a supported Python (installing Python 3.12 via winget on Windows if
// needed), installs the Python requirements into it, and installs the npm packages.
import { existsSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
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
const versionText = (version) => (version ? `${version.major}.${version.minor}` : 'unknown');

// Default python.org install folders, in case the py launcher is missing or not refreshed yet.
function windowsInstallPaths() {
  const roots = [
    process.env.LOCALAPPDATA && join(process.env.LOCALAPPDATA, 'Programs', 'Python'),
    process.env.ProgramFiles,
  ].filter(Boolean);
  return SUPPORTED_MINORS.flatMap((minor) => roots.map((root) => [join(root, `Python3${minor}`, 'python.exe')]));
}

function findPython() {
  const candidates = [
    ...(process.env.PYTHON ? [[process.env.PYTHON]] : []),
    ...(process.platform === 'win32' ? SUPPORTED_MINORS.map((minor) => ['py', `-3.${minor}`]) : []),
    ...(process.platform === 'win32' ? windowsInstallPaths() : []),
    ...SUPPORTED_MINORS.map((minor) => [`python3.${minor}`]),
    ['python3'],
    ['python'],
  ];
  const unsupported = new Set();
  for (const [cmd, ...args] of candidates) {
    const version = pythonVersion(cmd, args);
    if (!version) continue;
    if (isSupported(version)) return { cmd, args, version: `3.${version.minor}` };
    unsupported.add(versionText(version));
  }
  return { unsupported: [...unsupported] };
}

function installPythonWithWinget() {
  if (process.platform !== 'win32') return false;
  console.log('\n▶ Installing Python 3.12 with winget (your other Python versions are kept)');
  const result = spawnSync(
    'winget',
    ['install', '-e', '--id', 'Python.Python.3.12', '--scope', 'user', '--accept-package-agreements', '--accept-source-agreements'],
    { stdio: 'inherit' },
  );
  if (result.error) console.log('   winget is not available on this PC.');
  return !result.error;
}

// A .venv built with an unsupported Python (e.g. 3.14+) can never install the pinned packages.
if (existsSync(VENV_PYTHON)) {
  const version = pythonVersion(VENV_PYTHON);
  if (!isSupported(version)) {
    console.log(`\n▶ Removing .venv: it was made with Python ${versionText(version)}, which this project cannot use`);
    rmSync(VENV_DIR, { recursive: true, force: true });
  }
}

if (existsSync(VENV_PYTHON)) {
  console.log(`\n▶ Using existing virtual environment (${VENV_DIR})`);
} else {
  let python = findPython();
  if (!python.cmd && installPythonWithWinget()) python = findPython();
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
