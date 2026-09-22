#!/usr/bin/env node
// Writes the proof's fixture repository and commits it.
//
// The fixture is small on purpose, and its answer is known by construction:
// `add_tax` in pricing/tax.py is called from exactly two functions,
// `cart_total` in pricing/cart.py and `invoice_line` in pricing/invoice.py.
// Every byte is written here with LF line endings, so every platform admits
// the same tree.
//
// Usage: node make-fixture.mjs <empty-or-missing-directory>

import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const target = process.argv[2];
if (!target) {
  console.error('usage: make-fixture.mjs <directory>');
  process.exit(2);
}

const files = {
  'README.md': '# Pricing fixture\n\nA tiny repository with a known call graph.\n',
  'pricing/__init__.py': '"""Pricing helpers for the fixture."""\n',
  'pricing/tax.py': [
    '"""Tax arithmetic for the fixture."""',
    '',
    '',
    'def add_tax(amount, rate):',
    '    """Return amount with tax at rate applied."""',
    '    return round(amount * (1 + rate), 2)',
    '',
  ].join('\n'),
  'pricing/cart.py': [
    'from pricing.tax import add_tax',
    '',
    '',
    'def cart_total(prices):',
    '    """Sum prices after tax."""',
    '    return sum(add_tax(price, 0.08) for price in prices)',
    '',
  ].join('\n'),
  'pricing/invoice.py': [
    'from pricing.tax import add_tax',
    '',
    '',
    'def invoice_line(description, amount):',
    '    """Format one taxed invoice line."""',
    '    return f"{description}: {add_tax(amount, 0.2):.2f}"',
    '',
  ].join('\n'),
  'main.py': [
    'from pricing.cart import cart_total',
    'from pricing.invoice import invoice_line',
    '',
    '',
    'def main():',
    '    print(cart_total([10.0, 5.5]))',
    '    print(invoice_line("widget", 12.0))',
    '',
    '',
    'if __name__ == "__main__":',
    '    main()',
    '',
  ].join('\n'),
};

if (fs.existsSync(target) && fs.readdirSync(target).length > 0) {
  console.error(`make-fixture: ${target} exists and is not empty`);
  process.exit(1);
}
fs.mkdirSync(target, { recursive: true });
for (const [name, body] of Object.entries(files)) {
  const file = path.join(target, name);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, body, { encoding: 'utf8' });
}

const git = (...args) => execFileSync('git', args, { cwd: target, stdio: ['ignore', 'pipe', 'inherit'] }).toString();
git('init', '--quiet');
// Git for Windows enables autocrlf system-wide; Kin admits only a worktree whose
// bytes match the committed tree, so the fixture pins both settings locally.
git('config', 'core.autocrlf', 'false');
git('config', 'core.eol', 'lf');
git('config', 'user.name', 'Kin platform proof');
git('config', 'user.email', 'proof@example.invalid');
git('add', '--all');
// The fixture is throwaway local history, so a machine's own commit hooks have
// no say over it.
git('commit', '--quiet', '--no-verify', '-m', 'Add pricing fixture');
const head = git('rev-parse', 'HEAD').trim();
console.log(JSON.stringify({ fixture: path.resolve(target), head, files: Object.keys(files) }));
