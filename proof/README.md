# Windows and WSL MCP proof

This branch holds one workflow and the small client it runs. It installs Kin
on a fresh GitHub-hosted Windows runner the way a user would, then drives the
installed `kin mcp start` with the official MCP SDK client
(`@modelcontextprotocol/sdk`, pinned to 1.30.0 in `package-lock.json`). It
runs on this public repository's free runners and uses no secrets.

The client is the official MCP SDK client (1.30.0). It is not the Claude Code
app, and no receipt from this workflow shows the Claude Code app, or any other
named agent product, running.

It runs up to four legs: native Windows and WSL2 (Ubuntu 24.04), each installed
two ways.

- **archive** installs from an archive, and only after the archive's bytes
  match the expected sha256. It places the binaries where the archive's own
  `INSTALL.md` says.
- **npx** runs `npx -y @kinlab/kin@<version>`. It joins once npm carries the
  version. It also compares the bytes npm provisioned against the same archive.

The WSL job also records a fifth result: a Windows-side client starting Kin
inside WSL through `wsl.exe`. That result is informational and does not decide
the job.

## Which bytes

By default the archive is the published GitHub release asset for the version,
checked against the release's own `.sha256` sidecar.

A dispatch can name an exact candidate archive instead, so a candidate is
proven without publishing a release:

```
gh workflow run windows-wsl-mcp-proof.yml -R firelock-ai/kin-actions --ref ci/windows-wsl-mcp-proof \
  -f kin_version=<version the binaries report> \
  -f windows_archive_url=<https URL of a kin-windows-x86_64 .zip or .tar.gz> \
  -f windows_archive_sha256=<its sha256> \
  -f linux_archive_url=<https URL of a kin-linux-x86_64 .tar.gz> \
  -f linux_archive_sha256=<its sha256>
```

Either platform may be given alone; the other platform's archive legs are then
skipped. Every receipt records the source kind (`release`, `artifact` or
`npm`), the URL, the expected sha256 and where it came from, and the sha256
the downloaded bytes actually had.

Rules for a candidate archive, because this is a public runner:

- The URL must be anonymous `https` with no credentials, query string or
  fragment. A signed or tokened URL is refused before anything runs, so no
  credential can reach a public log. Workflow inputs are visible to anyone who
  can see the run.
- Only built archives come in. Nothing private is checked out, and the
  archive under test is never uploaded as an artifact. Only receipts are.
- The sha256 is required and is checked before anything is installed.

### Delivered as a local file

With `-f deliver_as_local_file=true`, each leg first receives its archive as a
local file and installs from that file only. The install checks the file's
sha256 before extracting anything and refuses on a mismatch. Every receipt
records the delivery under `source.transfer`: its kind, its reference, and a
note.

A private candidate arrives this way on a machine we control. It comes either
through an authenticated Actions artifact download in a private repository,
whose receipt records the private run id and artifact name, or through a
private copy. On this public runner a download inside the job stands in for
that delivery, recorded as `public-download-stand-in`.

### From a private workflow

Every job step runs a script under `proof/`, so a private workflow can check
out this branch at a pinned commit and run the same scripts.

- `deliver-stand-in.sh` is the public stand-in for a delivery.
- `install-windows.sh` and `install-linux.sh` install from `REF_FILE` or
  `REF_URL`, always against `REF_SHA256`.
- `leg-windows.sh`, `wsl-prepare.sh`, `leg-wsl.sh` and
  `leg-windows-to-wsl.ps1` run the MCP legs.
- `setup-contract.ps1` runs the setup contract.

The private workflow sets `REF_FILE` and the `TRANSFER_*` record itself after
its own delivery step.

A draft release asset cannot be fetched here. GitHub shows draft releases
only to accounts with push access. This runner's token belongs to this
repository, so it cannot see another repository's drafts. Reaching one would
mean storing a cross-repository credential as a secret in a public
repository, and anyone able to push a branch here could read it out.

The safest candidate source is a prerelease tag such as `v0.8.0-rc.1`. Kin's
own release workflow already publishes every tag as a prerelease that is not
Latest, with `.sha256` sidecars, so its assets are public and anonymous.

## The Windows kin setup client-failure contract

A separate job, graded apart from the MCP handshake, runs the installed
`kin setup --no-interactive --intent agent` against the real profile root on
native Windows. That folder is the parent of Claude Code's `~/.claude.json`.
Each run starts from a Claude Code config that a person already has. The
Claude Code app is not installed, and only the config file setup writes is
under test.

| Case | What holds the profile root | A fixed build must |
|---|---|---|
| control | nothing | exit 0, merge Kin's entry, keep the person's config |
| shell-in-profile | a real shell whose current directory is the profile root | exit 0, merge Kin's entry, keep the person's config |
| unmergeable-config | nothing, but `~/.claude.json` is not JSON | exit 1, name Claude Code on stderr, leave the file byte-identical, still finish the run |
| exclusive-hold | a handle that shares nothing | exit 1, name Claude Code on stderr, leave the file byte-identical |

Each hold is proven in force before setup runs, and released after, with a
probe that asks for `DELETE` on the profile root. A shell sitting in a folder
refuses that probe with a sharing violation, which is what blocked the older
setup's write.

`setup_contract_expect` says which behaviour to grade. `fixed` is the contract
above. `old` grades builds from before the fix, where every blocked write was
one "configuration failed" line and setup still exited 0. 0.7.21 predates the
fix, so `proof/target.env` grades it as `old`: that run is the negative
control.

## Run it

- Push a change to `proof/target.env` to prove a published version, or
- `gh workflow run windows-wsl-mcp-proof.yml -R firelock-ai/kin-actions --ref ci/windows-wsl-mcp-proof -f kin_version=<version>`,
  adding the archive inputs above for a candidate, and
  `-f setup_contract_expect=old` for a build from before the setup fix.

Each leg uploads its receipt as an artifact named `receipt-<leg>-<install>`.
The job summary shows the same grades as a table.

## What each level proves

A receipt grades four levels, and copies this wording into its `levels`
field. A level reads `proven` only when every check at that level passed.

| Level | Proven means |
|---|---|
| install | The user-style install produced a binary that runs and reports the requested version, from bytes whose sha256 matched the recorded source. |
| config written | `kin setup --no-interactive --intent agent` wrote Claude Code's MCP config file (`~/.claude.json`) naming that binary. Only the file is proven. Claude Code is detected from a seeded `~/.claude/settings.json`; the Claude Code app was not installed and did not run. |
| MCP handshake | The official MCP SDK client (1.30.0), launching the server exactly as that config entry names it, completed `initialize` and `tools/list`. `serverInfo` reports the requested version and the tool list includes `find_references`. This is not the Claude Code app. |
| real tool answer | `find_references` for `add_tax` answered without error from the fixture's graph. It named both callers the fixture was built with, `cart_total` and `invoice_line`. |

## What it does not prove

- That the Claude Code app, or any other named agent product, connects.
- Anything embedding-backed. `find_references` is answered from graph edges.
- Filesystem projection, review workflows, long-running daemon behaviour, or
  performance.
- WSL1, Windows on ARM64, Windows 11, or any machine other than a
  GitHub-hosted runner.
