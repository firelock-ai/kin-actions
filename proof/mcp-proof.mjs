#!/usr/bin/env node
// Drives an installed Kin MCP server with the official MCP TypeScript SDK
// client and writes a JSON receipt of what ran and what it answered.
//
// The server is started the way an agent starts it: the command, arguments
// and environment come from a client configuration entry that `kin setup`
// wrote, and the working directory is the fixture repository. The explicit
// launch mode exists for one case only, a Windows-side client starting Kin
// inside WSL through wsl.exe, where no setup-written entry applies.
//
// Every claim in the receipt is graded at one of four levels, and a level is
// reported as proven only when every check at that level passed:
//   install          the user-style install produced a binary that runs and
//                    reports the requested version
//   config_written   `kin setup` wrote a client MCP entry that names that binary
//   mcp_handshake    an SDK client completed initialize and tools/list against
//                    the server that entry starts
//   real_tool_answer find_references answered from the fixture's graph with the
//                    two callers the fixture was built to have

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

function arg(name, fallback = undefined) {
  const index = process.argv.indexOf(`--${name}`);
  return index === -1 ? fallback : process.argv[index + 1];
}

// JSON arguments may be given inline or as @file, because quoting JSON through
// PowerShell or Git Bash into a native process is its own source of failure.
function jsonArg(name) {
  const value = arg(name);
  if (value === undefined) return undefined;
  return JSON.parse(value.startsWith('@') ? fs.readFileSync(value.slice(1), 'utf8') : value);
}

const options = {
  leg: arg('leg', 'unknown'),
  installMode: arg('install-mode', 'unknown'),
  kinVersion: arg('kin-version'),
  repo: arg('repo'),
  launch: arg('launch', 'config'),
  config: arg('config'),
  serverName: arg('server-name', 'kin'),
  command: arg('command'),
  args: jsonArg('args-json'),
  binary: arg('binary'),
  versionCommand: jsonArg('version-command-json'),
  installJson: arg('install-json'),
  initStatus: arg('init-status'),
  setupStatus: arg('setup-status'),
  symbol: arg('symbol', 'add_tax'),
  expect: (arg('expect', 'cart_total,invoice_line') || '').split(',').filter(Boolean),
  out: arg('out'),
  timeoutMs: Number(arg('timeout-ms', '240000')),
};

if (!options.kinVersion || !options.repo || !options.out) {
  console.error('usage: mcp-proof.mjs --kin-version X --repo DIR --out FILE [--launch config --config FILE | --launch explicit --command C --args-json JSON] ...');
  process.exit(2);
}

const started = Date.now();
const receipt = {
  schema: 'kin-platform-mcp-receipt/1',
  leg: options.leg,
  install_mode: options.installMode,
  kin_version_requested: options.kinVersion,
  recorded_at: new Date().toISOString(),
  host: {
    platform: process.platform,
    arch: process.arch,
    os_release: os.release(),
    os_version: typeof os.version === 'function' ? os.version() : null,
    node: process.version,
    runner_image: process.env.ImageOS ? `${process.env.ImageOS} ${process.env.ImageVersion ?? ''}`.trim() : null,
    wsl_distro: process.env.WSL_DISTRO_NAME ?? null,
    user: os.userInfo().username,
  },
  install: null,
  binary: null,
  config: null,
  handshake: null,
  tool_answer: null,
  checks: [],
  proves: {},
  not_proven: [
    'a named agent product (Claude Code, Cursor, Codex) connecting; the client here is the official MCP TypeScript SDK',
    'semantic_locate or any embedding-backed answer; find_references is graph-edge only',
    'filesystem projection, review workflows, or long-running daemon behaviour',
  ],
  wire: [],
};

function check(level, name, pass, detail = null) {
  receipt.checks.push({ level, name, pass: Boolean(pass), detail });
  const mark = pass ? 'PASS' : 'FAIL';
  console.log(`[${level}] ${mark} ${name}${detail === null ? '' : `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`}`);
  return Boolean(pass);
}

function sha256File(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function sameFile(a, b) {
  try {
    return fs.realpathSync(a).toLowerCase() === fs.realpathSync(b).toLowerCase() || sha256File(a) === sha256File(b);
  } catch {
    return false;
  }
}

function stripVerbatim(p) {
  return typeof p === 'string' && p.startsWith('\\\\?\\') ? p.slice(4) : p;
}

function writeReceipt() {
  receipt.elapsed_ms = Date.now() - started;
  for (const level of ['install', 'config_written', 'mcp_handshake', 'real_tool_answer']) {
    const at = receipt.checks.filter((c) => c.level === level);
    receipt.proves[level] = at.length === 0 ? 'not_attempted' : at.every((c) => c.pass) ? 'proven' : 'failed';
  }
  fs.mkdirSync(path.dirname(path.resolve(options.out)), { recursive: true });
  fs.writeFileSync(options.out, `${JSON.stringify(receipt, null, 2)}\n`);
  console.log(`receipt: ${path.resolve(options.out)}`);
  console.log(JSON.stringify(receipt.proves));
}

// ---------------------------------------------------------------------------
// install: which binary, which bytes, which version
// ---------------------------------------------------------------------------
if (options.installJson && fs.existsSync(options.installJson)) {
  receipt.install = JSON.parse(fs.readFileSync(options.installJson, 'utf8'));
  for (const c of receipt.install.checks ?? []) check('install', c.name, c.pass, c.detail ?? null);
}

// Exit statuses of the kin commands the workflow ran before this client, each
// read from a file so a failed step still yields a graded receipt.
function stepStatus(file) {
  if (!file) return null;
  try {
    return Number(fs.readFileSync(file, 'utf8').trim());
  } catch {
    return NaN;
  }
}
const setupExit = stepStatus(options.setupStatus);
if (setupExit !== null) check('config_written', '`kin setup --no-interactive --intent agent` exited 0', setupExit === 0, setupExit);
const initExit = stepStatus(options.initStatus);
if (initExit !== null) check('real_tool_answer', '`kin init` admitted the fixture (exit 0)', initExit === 0, initExit);

let launch = null;
let launchedFrom = options.launch;
if (options.launch === 'config') {
  let entry = null;
  let configText = null;
  try {
    configText = fs.readFileSync(options.config, 'utf8');
    entry = JSON.parse(configText)?.mcpServers?.[options.serverName] ?? null;
  } catch (error) {
    check('config_written', 'client config is readable JSON', false, `${options.config}: ${error.message}`);
  }
  receipt.config = { path: options.config, entry, sha256: configText ? crypto.createHash('sha256').update(configText).digest('hex') : null };
  if (configText !== null) check('config_written', 'client config is readable JSON', true, options.config);
  const hasEntry = entry && typeof entry.command === 'string' && Array.isArray(entry.args);
  check('config_written', `config names an mcpServers.${options.serverName} entry with command and args`, hasEntry, entry);
  if (hasEntry) {
    check('config_written', 'entry starts the server with `mcp start`', entry.args[0] === 'mcp' && entry.args[1] === 'start', entry.args);
    if (options.binary) {
      check('config_written', 'entry command is the installed binary', sameFile(entry.command, options.binary), { command: entry.command, installed: options.binary });
    }
    launch = { command: entry.command, args: entry.args, env: entry.env ?? {}, cwd: stripVerbatim(entry.cwd) ?? options.repo };
  }
} else {
  launch = { command: options.command, args: options.args ?? [], env: {}, cwd: options.repo };
  receipt.config = { launch: 'explicit', note: 'no setup-written entry applies to this leg' };
}

const binaryPath = options.binary ?? null;
const versionArgv = options.versionCommand
  ? options.versionCommand
  : binaryPath
    ? [binaryPath, '--version']
    : null;
if (versionArgv) {
  const ran = spawnSync(versionArgv[0], versionArgv.slice(1), { encoding: 'utf8', timeout: 60_000, windowsHide: true });
  const output = `${ran.stdout ?? ''}${ran.stderr ?? ''}`.trim();
  receipt.binary = {
    path: binaryPath,
    resolved: binaryPath && fs.existsSync(binaryPath) ? fs.realpathSync(binaryPath) : null,
    sha256: binaryPath && fs.existsSync(binaryPath) ? sha256File(binaryPath) : (receipt.install?.binary_sha256 ?? null),
    size_bytes: binaryPath && fs.existsSync(binaryPath) ? fs.statSync(binaryPath).size : null,
    version_argv: versionArgv,
    version_output: output,
    version_exit: ran.status,
  };
  check('install', 'installed binary runs `--version`', ran.status === 0, output.split(/\r?\n/)[0] ?? '');
  check('install', `version output names ${options.kinVersion}`, new RegExp(`\\b${options.kinVersion.replace(/\./g, '\\.')}\\b`).test(output), output.split(/\r?\n/)[0] ?? '');
}

// With no readable setup-written entry, start the installed binary directly
// so the handshake and the tool answer are still measured. config_written has
// already failed above, and the receipt says which launch was used.
if (!launch && options.binary) {
  launch = { command: options.binary, args: ['mcp', 'start'], env: {}, cwd: options.repo };
  launchedFrom = 'fallback: installed binary `mcp start`, because no setup-written entry was readable';
}
if (!launch) {
  writeReceipt();
  process.exit(1);
}

// ---------------------------------------------------------------------------
// mcp_handshake and real_tool_answer, through the official SDK client
// ---------------------------------------------------------------------------
const transport = new StdioClientTransport({
  command: launch.command,
  args: launch.args,
  env: { ...(launch.env ?? {}) },
  cwd: launch.cwd,
  stderr: 'pipe',
});
let stderrText = '';
transport.stderr?.on('data', (chunk) => {
  stderrText += chunk.toString('utf8');
  if (stderrText.length > 200_000) stderrText = stderrText.slice(-200_000);
});

// Record the wire in both directions, trimmed, so the receipt shows the real
// initialize exchange rather than what the SDK chose to surface.
const record = (direction, message) => {
  const text = JSON.stringify(message);
  receipt.wire.push({ t_ms: Date.now() - started, direction, bytes: Buffer.byteLength(text), message: text.length > 4000 ? `${text.slice(0, 4000)}...` : text });
};
const originalSend = transport.send.bind(transport);
transport.send = async (message, sendOptions) => {
  record('client->server', message);
  return originalSend(message, sendOptions);
};
let inbound = null;
Object.defineProperty(transport, 'onmessage', {
  configurable: true,
  get: () => inbound,
  set: (handler) => {
    inbound = handler === undefined ? undefined : (message, extra) => {
      record('server->client', message);
      return handler(message, extra);
    };
  },
});

const client = new Client({ name: 'kin-platform-proof', version: '1.0.0' }, { capabilities: {} });
const deadline = setTimeout(() => {
  check('mcp_handshake', `proof finished within ${options.timeoutMs} ms`, false, stderrText.slice(-2000));
  writeReceipt();
  process.exit(1);
}, options.timeoutMs);

let exitCode = 0;
try {
  const t0 = Date.now();
  await client.connect(transport, { timeout: 120_000 });
  const initialize = receipt.wire.find((w) => w.direction === 'server->client' && w.message.includes('"protocolVersion"'));
  const initResult = initialize ? JSON.parse(initialize.message.endsWith('...') ? '{}' : initialize.message).result ?? {} : {};
  const serverInfo = client.getServerVersion() ?? null;
  receipt.handshake = {
    launched_from: launchedFrom,
    launched: { command: launch.command, args: launch.args, cwd: launch.cwd },
    server_pid: transport.pid,
    connect_ms: Date.now() - t0,
    protocol_version: initResult.protocolVersion ?? null,
    server_info: serverInfo,
    capabilities: Object.keys(client.getServerCapabilities() ?? {}),
    instructions_bytes: Buffer.byteLength(client.getInstructions() ?? ''),
  };
  check('mcp_handshake', 'SDK client completed initialize', true, serverInfo);
  check('mcp_handshake', 'server negotiated a protocol version', Boolean(initResult.protocolVersion), initResult.protocolVersion ?? null);
  const reported = [serverInfo?.version, serverInfo?.kinVersion].filter(Boolean);
  check('mcp_handshake', `serverInfo reports ${options.kinVersion}`, reported.includes(options.kinVersion), reported);

  const t1 = Date.now();
  const tools = await client.listTools(undefined, { timeout: 120_000 });
  const names = (tools.tools ?? []).map((t) => t.name);
  const findRefs = (tools.tools ?? []).find((t) => t.name === 'find_references') ?? null;
  receipt.handshake.tools = { count: names.length, names, list_ms: Date.now() - t1, find_references_input_schema: findRefs?.inputSchema ?? null };
  check('mcp_handshake', 'tools/list returned tools', names.length > 0, names.length);
  check('mcp_handshake', 'tools/list includes find_references', Boolean(findRefs));

  // find_references answers "still starting" while the repository daemon comes
  // up. Retry that one answer, the way an agent would, and nothing else.
  const args = { query: options.symbol };
  let attempts = 0;
  let result = null;
  const t2 = Date.now();
  while (Date.now() - t2 < options.timeoutMs - 30_000) {
    attempts += 1;
    result = await client.callTool({ name: 'find_references', arguments: args }, undefined, { timeout: 180_000 });
    const text = (result.content ?? []).map((c) => c.text ?? '').join('\n');
    if (!/still starting/i.test(text)) break;
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  const text = (result?.content ?? []).map((c) => c.text ?? '').join('\n');
  let answer = null;
  try {
    answer = JSON.parse(text);
  } catch {
    answer = null;
  }
  // The primary collection is named by the envelope itself; `references` is
  // what 0.7.21 names it, and each row carries the referencing entity.
  const primary = answer?._kin?.response?.primary_collection ?? 'references';
  const rows = Array.isArray(answer?.[primary]) ? answer[primary] : [];
  const found = rows.map((row) => ({
    name: row?.name ?? null,
    kind: row?.kind ?? null,
    file_path: row?.file_path ?? null,
    relation_kinds: row?.relation_kinds ?? null,
    reference_lines: row?.reference_lines ?? null,
    resolution: row?.resolution ?? null,
  }));
  const foundNames = new Set(found.map((f) => f.name).filter(Boolean));
  const focal = answer?.focal_entity ?? null;
  receipt.tool_answer = {
    tool: 'find_references',
    arguments: args,
    attempts,
    elapsed_ms: Date.now() - t2,
    is_error: result?.isError === true,
    answer_bytes: Buffer.byteLength(text),
    answer_sha256: crypto.createHash('sha256').update(text).digest('hex'),
    focal_entity: focal,
    primary_collection: primary,
    references: found,
    counts: answer?.counts ?? null,
    expected_callers: options.expect,
    verdict: answer?._kin?.verdict ?? null,
    completeness: answer?._kin?.completeness ?? null,
    runtime: answer?._kin?.runtime ?? null,
    graph_state: answer?._kin?.graph_state ?? null,
    answer_excerpt: text.length > 8000 ? `${text.slice(0, 8000)}...` : text,
  };
  check('real_tool_answer', 'find_references returned a non-error result', result && result.isError !== true, result?.isError === true ? text.slice(0, 500) : null);
  check('real_tool_answer', 'answer is Kin JSON with a _kin envelope', Boolean(answer?._kin), answer ? Object.keys(answer) : text.slice(0, 300));
  check('real_tool_answer', `focal entity is ${options.symbol} in pricing/tax.py`,
    focal?.name === options.symbol && /pricing[\\/]tax\.py$/.test(focal?.file_path ?? ''), focal ? { name: focal.name, file_path: focal.file_path } : null);
  for (const caller of options.expect) {
    check('real_tool_answer', `answer names the known caller ${caller}`, foundNames.has(caller), [...foundNames]);
  }
} catch (error) {
  exitCode = 1;
  check(receipt.handshake ? 'real_tool_answer' : 'mcp_handshake', 'MCP session completed without a client error', false, `${error.message}\n${stderrText.slice(-3000)}`);
} finally {
  clearTimeout(deadline);
  receipt.server_stderr_tail = stderrText.slice(-4000);
  try {
    await client.close();
  } catch {
    // The server exiting first is fine; the receipt already has what it said.
  }
}

writeReceipt();
const failed = receipt.checks.filter((c) => !c.pass);
process.exit(exitCode || (failed.length > 0 ? 1 : 0));
