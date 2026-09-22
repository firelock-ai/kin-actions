#!/usr/bin/env node
// Writes install.json: where the installed bytes came from, which sha256 they
// had to match, and the install-level checks the proof client grades.
//
// Everything arrives through the environment the workflow step sets:
//   INSTALL_MODE   archive | npx
//   KIND           release | artifact | npm (where the installed bytes came from)
//   SOURCE_KIND    release | artifact (where the reference archive came from)
//   REF_URL        the reference archive, empty when there is none
//   REF_SHA256     the sha256 it had to match: the release sidecar or the input
//   DOWNLOADED     the sha256 its downloaded bytes had
//   ARCHIVE_KIN    the sha256 of the kin binary inside it
//   INSTALLED      the installed binary's path, empty when nothing was installed
//   INSTALLED_SHA  that binary's sha256
//   BINARY_NAME    kin.exe | kin
//   HOW            one sentence saying what the install did
//   PROOF_KIN_VERSION, OUT
import fs from 'node:fs';

const e = process.env;
const hasRef = Boolean(e.REF_URL);
const expectedFrom = e.SOURCE_KIND === 'artifact' ? 'the sha256 given as a workflow input' : "the release's published .sha256 sidecar";
const checks = [];

if (hasRef) {
  const name = e.INSTALL_MODE === 'archive'
    ? (e.SOURCE_KIND === 'artifact'
      ? 'candidate archive matches the sha256 given as a workflow input'
      : "release archive matches its published .sha256 sidecar")
    : `reference archive matches ${expectedFrom}`;
  checks.push({ name, pass: e.DOWNLOADED === e.REF_SHA256, detail: { url: e.REF_URL, expected: e.REF_SHA256, downloaded: e.DOWNLOADED } });
}
checks.push({ name: `an installed ${e.BINARY_NAME} exists`, pass: Boolean(e.INSTALLED), detail: e.INSTALLED || null });
if (hasRef) {
  checks.push({
    name: e.INSTALL_MODE === 'npx'
      ? `npm provisioned the same ${e.BINARY_NAME} bytes as the reference archive`
      : `installed ${e.BINARY_NAME} is byte-identical to the one inside the archive`,
    pass: Boolean(e.INSTALLED_SHA) && e.INSTALLED_SHA === e.ARCHIVE_KIN,
    detail: { installed: e.INSTALLED_SHA || null, archive: e.ARCHIVE_KIN || null },
  });
}

const out = {
  method: e.INSTALL_MODE,
  how: e.HOW,
  source: {
    kind: e.KIND,
    archive_url: e.REF_URL || null,
    archive_role: hasRef ? (e.INSTALL_MODE === 'archive' ? 'installed' : 'comparison only') : null,
    archive_sha256_expected: e.REF_SHA256 || null,
    archive_sha256_expected_from: hasRef ? expectedFrom : null,
    archive_sha256_downloaded: e.DOWNLOADED || null,
    npm_spec: e.INSTALL_MODE === 'npx' ? `@kinlab/kin@${e.PROOF_KIN_VERSION}` : null,
  },
  binary_path: e.INSTALLED || null,
  binary_sha256: e.INSTALLED_SHA || null,
  checks,
};
fs.writeFileSync(e.OUT, `${JSON.stringify(out, null, 2)}\n`);
console.log(JSON.stringify(out, null, 2));
