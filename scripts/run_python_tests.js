#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const candidates = [
  process.env.PYTHON,
  path.join(root, '.venv', 'bin', 'python'),
  path.join(root, '.venv', 'Scripts', 'python.exe'),
  path.join(root, 'venv', 'bin', 'python'),
  path.join(root, 'venv', 'Scripts', 'python.exe'),
  process.platform === 'win32' ? 'python' : 'python3',
].filter(Boolean);

const python = candidates.find((candidate) => {
  if (!candidate.includes(path.sep)) return true;
  return fs.existsSync(candidate);
});

const result = spawnSync(
  python,
  ['-m', 'unittest', 'discover', '-s', 'test', '-p', 'test_*.py', '-v'],
  { cwd: root, stdio: 'inherit' },
);

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
