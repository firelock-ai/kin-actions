# Windows and WSL MCP proof

This branch holds one workflow and the small client it runs. It installs a
published Kin release on a fresh GitHub-hosted Windows runner, the way a user
would, and drives the installed `kin mcp start` with the official MCP
TypeScript SDK client. It runs on this public repository's free runners and
uses no secrets.

It runs four legs per release: native Windows and WSL2 (Ubuntu 24.04), each
installed two ways. npx means `npx -y @kinlab/kin@<version>`. Archive means
the release's own zip or tarball, checked against its `.sha256` sidecar and
placed where the archive's `INSTALL.md` says. The npx legs run only once npm
carries the version. The WSL job also records a fifth result: a Windows-side
client starting Kin inside WSL through `wsl.exe`. That result is informational
and does not decide the job.

## Run it

- Push a change to `proof/target.env` naming the version, or
- `gh workflow run windows-wsl-mcp-proof.yml -R firelock-ai/kin-actions --ref ci/windows-wsl-mcp-proof -f kin_version=<version>`

Each leg uploads its receipt as an artifact named `receipt-<leg>-<install>`.
The job summary shows the same grades as a table.

## What each level proves

A receipt grades four levels. A level reads `proven` only when every check at
that level passed.

| Level | Proven means |
|---|---|
| install | The user-style install produced a binary that runs and reports the requested version. Its bytes match the published archive's binary. |
| config written | `kin setup --no-interactive --intent agent` exited 0 and wrote a Claude Code MCP entry (`~/.claude.json`) whose command is that installed binary. Claude Code is detected from a seeded `~/.claude/settings.json`; the Claude Code app is not installed. |
| MCP handshake | The SDK client, launching the server exactly as that entry names it, completed `initialize` and `tools/list`. `serverInfo` reports the requested version and the tool list includes `find_references`. |
| real tool answer | `find_references` for `add_tax` answered without error from the fixture's graph. It named both callers the fixture was built with, `cart_total` and `invoice_line`. |

## What it does not prove

- That a named agent product connects. The client is the official MCP SDK,
  not Claude Code, Cursor, or Codex.
- Anything embedding-backed. `find_references` is answered from graph edges.
- Filesystem projection, review workflows, long-running daemon behaviour, or
  performance.
- WSL1, Windows on ARM64, or any machine other than a GitHub-hosted runner.
