#!/usr/bin/env node
// Prints receipts as a Markdown table for the job summary.
// Usage: node summarize.mjs <receipt.json>...
import fs from 'node:fs';

const rows = [];
for (const file of process.argv.slice(2)) {
  if (!fs.existsSync(file)) continue;
  const r = JSON.parse(fs.readFileSync(file, 'utf8'));
  const refs = (r.tool_answer?.references ?? []).map((x) => `${x.name} (${x.file_path})`).join(', ') || 'none';
  rows.push([
    r.leg,
    r.install_mode,
    (r.binary?.version_output ?? '').split(/\r?\n/)[0] || 'unknown',
    r.binary?.sha256 ? r.binary.sha256.slice(0, 16) : 'unknown',
    r.proves.install,
    r.proves.config_written,
    r.proves.mcp_handshake,
    r.proves.real_tool_answer,
    `${r.handshake?.server_info?.name ?? '?'} ${r.handshake?.server_info?.version ?? '?'}, protocol ${r.handshake?.protocol_version ?? '?'}, ${r.handshake?.tools?.count ?? '?'} tools`,
    `${refs}; verdict ${r.tool_answer?.verdict?.state ?? 'unknown'}`,
  ]);
}

console.log('| leg | install | kin --version | binary sha256 | install | config written | MCP handshake | real tool answer | server | find_references(add_tax) |');
console.log('|---|---|---|---|---|---|---|---|---|---|');
for (const row of rows) console.log(`| ${row.map((cell) => String(cell).replace(/\|/g, '/')).join(' | ')} |`);
if (rows.length === 0) console.log('| no receipt was written | | | | | | | | | |');
