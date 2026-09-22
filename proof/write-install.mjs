#!/usr/bin/env node
// Writes install.json: where the installed bytes came from, how they arrived,
// which sha256 they had to match, and the install-level checks the proof
// client grades.
//
// Everything arrives through the environment the install script sets:
//   INSTALL_MODE   archive | npx
//   KIND           release | artifact | local-file | npm (where the installed bytes came from)
//   SOURCE_KIND    release | artifact | local-file (where the reference archive came from)
//   REF_URL        the reference archive's URL, empty for a local file or none
//   REF_FILE       the reference archive's local path, empty unless it was delivered as a file
//   REF_SHA256     the sha256 it had to match
//   DOWNLOADED     the sha256 its bytes actually had
//   REFUSED        "true" when the archive was refused before extraction or install
//   ARCHIVE_KIN    the sha256 of the kin binary inside it
//   INSTALLED      the installed binary's path, empty when nothing was installed
//   INSTALLED_SHA  that binary's sha256
//   BINARY_NAME    kin.exe | kin
//   HOW            one sentence saying what the install did
//   TRANSFER_KIND, TRANSFER_REF, TRANSFER_NOTE
//                  how a local file got here: the private run id and artifact,
//                  the private copy, or the public stand-in download
//   PROOF_KIN_VERSION, OUT
import fs from 'node:fs';

const e = process.env;
const hasRef = Boolean(e.REF_URL || e.REF_FILE);
const expectedFrom = {
  release: "the release's published .sha256 sidecar",
  artifact: 'the sha256 given as a workflow input',
  'local-file': 'the sha256 given with the delivered file',
}[e.SOURCE_KIND] ?? 'unknown';
const checks = [];

if (hasRef) {
  const name = e.INSTALL_MODE === 'archive'
    ? {
      release: "release archive matches its published .sha256 sidecar",
      artifact: 'candidate archive matches the sha256 given as a workflow input',
      'local-file': 'delivered local archive matches the sha256 given with it (checked before extraction)',
    }[e.SOURCE_KIND]
    : `reference archive matches ${expectedFrom}`;
  checks.push({
    name,
    pass: e.DOWNLOADED === e.REF_SHA256,
    detail: { url: e.REF_URL || null, path: e.REF_FILE || null, expected: e.REF_SHA256, actual: e.DOWNLOADED, refused_before_install: e.REFUSED === 'true' },
  });
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
    archive_url: e.REF_URL && !e.REF_FILE ? e.REF_URL : null,
    archive_path: e.REF_FILE || null,
    archive_role: hasRef ? (e.INSTALL_MODE === 'archive' ? 'installed' : 'comparison only') : null,
    archive_sha256_expected: e.REF_SHA256 || null,
    archive_sha256_expected_from: hasRef ? expectedFrom : null,
    archive_sha256_actual: e.DOWNLOADED || null,
    refused_before_install: e.REFUSED === 'true',
    npm_spec: e.INSTALL_MODE === 'npx' ? `@kinlab/kin@${e.PROOF_KIN_VERSION}` : null,
    transfer: e.TRANSFER_KIND
      ? { kind: e.TRANSFER_KIND, reference: e.TRANSFER_REF || null, note: e.TRANSFER_NOTE || null }
      : null,
  },
  binary_path: e.INSTALLED || null,
  binary_sha256: e.INSTALLED_SHA || null,
  checks,
};
fs.writeFileSync(e.OUT, `${JSON.stringify(out, null, 2)}\n`);
console.log(JSON.stringify(out, null, 2));
