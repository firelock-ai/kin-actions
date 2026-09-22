#!/usr/bin/env node
// Prints receipts as a Markdown table for the job summary.
// Usage: node summarize.mjs <receipt.json>...
import fs from 'node:fs';

const rows = [];
const contracts = [];
let client = null;
for (const file of process.argv.slice(2)) {
  if (!fs.existsSync(file)) continue;
  const r = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (r.schema === 'kin-windows-setup-contract/1') {
    contracts.push(r);
    continue;
  }
  client = client ?? r.client?.label ?? null;
  const refs = (r.tool_answer?.references ?? []).map((x) => `${x.name} (${x.file_path})`).join(', ') || 'none';
  const source = r.source
    ? `${r.source.kind}: ${r.source.archive_url ?? r.source.npm_spec ?? 'n/a'}${r.source.archive_sha256_downloaded ? ` sha256 ${r.source.archive_sha256_downloaded.slice(0, 16)}` : ''}`
    : 'n/a';
  rows.push([
    r.leg,
    r.install_mode,
    source,
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

for (const r of contracts) {
  console.log(`### kin setup client-failure contract, graded as \`${r.expect}\`: ${r.contract_met ? 'met' : 'NOT met'}`);
  console.log('');
  console.log(`Binary: ${(r.binary?.version_output ?? '').split(/\r?\n/)[0]} sha256 ${r.binary?.sha256 ?? 'unknown'}; source ${r.source?.kind ?? 'n/a'} ${r.source?.archive_url ?? ''}`);
  console.log('');
  console.log('| case | hold | DELETE probe during hold | exit | Kin entry written | existing config kept | client failure message | met |');
  console.log('|---|---|---|---|---|---|---|---|');
  for (const c of r.cases ?? []) {
    console.log(`| ${c.name} | ${c.hold?.kind} | ${c.hold?.probe_delete_during} | ${c.exit_code} | ${c.kin_entry_present} | ${c.user_content_preserved} | ${String(c.failure_lines || 'none').replace(/\|/g, '/')} | ${c.contract_met} |`);
  }
  console.log('');
  console.log(r.level_meaning);
  console.log('');
}
if (rows.length === 0 && contracts.length > 0) process.exit(0);

console.log('| leg | install | source | kin --version | binary sha256 | install | config written | MCP handshake | real tool answer | server | find_references(add_tax) |');
console.log('|---|---|---|---|---|---|---|---|---|---|---|');
for (const row of rows) console.log(`| ${row.map((cell) => String(cell).replace(/\|/g, '/')).join(' | ')} |`);
if (rows.length === 0) console.log('| no receipt was written | | | | | | | | | | |');
console.log('');
console.log(`The MCP client is the ${client ?? 'official MCP SDK client'}, not the Claude Code app. "Config written" means kin setup wrote Claude Code's config file; the Claude Code app was not installed and did not run.`);
