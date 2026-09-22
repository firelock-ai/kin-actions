# Native Windows `kin setup` client-failure contract.
#
# Drives the installed `kin setup --no-interactive --intent agent` four times
# against the real profile root, the parent of Claude Code's ~/.claude.json:
#
#   control             nothing holds the profile root; setup must succeed and
#                       merge Kin's entry into the existing config
#   shell-in-profile    a real shell process sits in the profile root, holding
#                       it as its current directory the way a new terminal does
#   unmergeable-config  ~/.claude.json is not JSON, so Kin must refuse to write
#   exclusive-hold      the profile root is held with no sharing at all, which
#                       refuses even a read-only open of it
#
# -Expect fixed grades the contract a fixed build must meet: the shell hold no
# longer blocks the write, and a client that genuinely cannot be written makes
# setup exit 1 naming it, with its existing config left exactly as it was.
# -Expect old grades the behaviour of builds before that fix: every blocked
# write is one "configuration failed" line and the run still exits 0. Running
# old bytes with -Expect old is the negative control that shows the harness can
# tell the two apart.
#
# Every hold is proven in force before setup runs and released after, with a
# probe that asks for DELETE on the profile root, so no case can pass because
# its hold silently did not happen.

param(
  [Parameter(Mandatory)] [string] $Kin,
  [Parameter(Mandatory)] [string] $KinVersion,
  [Parameter(Mandatory)] [ValidateSet('fixed', 'old')] [string] $Expect,
  [Parameter(Mandatory)] [string] $Out,
  [Parameter(Mandatory)] [string] $Captures,
  [string] $InstallJson = ''
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $Captures | Out-Null

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class KinProofNative {
  [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);

  const uint DELETE = 0x00010000;
  const uint READ_CONTROL = 0x00020000;
  const uint GENERIC_READ = 0x80000000;
  const uint FILE_READ_ATTRIBUTES = 0x0080;
  const uint FILE_SHARE_READ = 1, FILE_SHARE_WRITE = 2, FILE_SHARE_DELETE = 4;
  const uint OPEN_EXISTING = 3;
  const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
  const uint FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000;

  static int Probe(string path, uint access) {
    using (var handle = CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, IntPtr.Zero, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, IntPtr.Zero)) {
      return handle.IsInvalid ? Marshal.GetLastWin32Error() : 0;
    }
  }

  // 0 when a DELETE open of the directory is granted, else the Win32 error.
  // 32 is ERROR_SHARING_VIOLATION: another handle withholds FILE_SHARE_DELETE.
  public static int ProbeDeleteOpen(string path) { return Probe(path, DELETE); }

  // 0 when a read-only open of the directory is granted, else the Win32 error.
  public static int ProbeReadOpen(string path) { return Probe(path, GENERIC_READ | FILE_READ_ATTRIBUTES | READ_CONTROL); }

  // Holds the directory with no sharing, so every other open of it is refused.
  public static SafeFileHandle OpenExclusive(string path, out int error) {
    var handle = CreateFileW(path, GENERIC_READ, 0, IntPtr.Zero, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, IntPtr.Zero);
    error = handle.IsInvalid ? Marshal.GetLastWin32Error() : 0;
    return handle;
  }
}
'@

$profileRoot = $env:USERPROFILE
$claudeConfig = Join-Path $profileRoot '.claude.json'
$setupCwd = Join-Path $env:RUNNER_TEMP 'setup-contract-cwd'
New-Item -ItemType Directory -Force -Path $setupCwd | Out-Null

# The same isolation the upstream wizard tests use: no daemon, no projection.
$env:KIN_NO_DAEMON = '1'
$env:KIN_VFS_DISABLE = '1'

# A Claude Code config a person already has. Kin must add its entry beside
# these, or leave the file exactly as it was, and never lose any of it.
$userSeed = @'
{
  "numStartups": 3,
  "userSetting": "keep-me",
  "mcpServers": {
    "other-tool": { "command": "other-tool.exe", "args": ["serve"] }
  }
}
'@
$unmergeableSeed = "{ this is not json`n"

function Get-Sha256Hex([string] $path) {
  if (-not (Test-Path -LiteralPath $path)) { return $null }
  return (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
}

function Write-Seed([string] $content) {
  [System.IO.File]::WriteAllText($claudeConfig, $content, [System.Text.UTF8Encoding]::new($false))
}

function Invoke-Setup([string] $label) {
  $stdout = Join-Path $Captures "$label.stdout.txt"
  $stderr = Join-Path $Captures "$label.stderr.txt"
  $process = Start-Process -FilePath $Kin -PassThru -NoNewWindow `
    -ArgumentList @('setup', '--no-interactive', '--intent', 'agent', '--shell', 'powershell', '--skip-mcp-check') `
    -WorkingDirectory $setupCwd -RedirectStandardOutput $stdout -RedirectStandardError $stderr
  $null = $process.Handle
  if (-not $process.WaitForExit(180000)) {
    $process.Kill($true)
    throw "kin setup ($label) did not finish within 180 seconds"
  }
  return [ordered]@{
    exit_code = $process.ExitCode
    stdout = [System.IO.File]::ReadAllText($stdout)
    stderr = [System.IO.File]::ReadAllText($stderr)
  }
}

function Read-Config() {
  $raw = if (Test-Path -LiteralPath $claudeConfig) { [System.IO.File]::ReadAllText($claudeConfig) } else { $null }
  $json = $null
  if ($null -ne $raw) { try { $json = $raw | ConvertFrom-Json -AsHashtable } catch { $json = $null } }
  return [ordered]@{ raw = $raw; json = $json; sha256 = Get-Sha256Hex $claudeConfig }
}

function Test-UserContent($json) {
  return ($null -ne $json) -and ($json['userSetting'] -eq 'keep-me') -and ($json['numStartups'] -eq 3) -and
    ($null -ne $json['mcpServers']) -and ($null -ne $json['mcpServers']['other-tool'])
}

function Test-KinEntry($json) {
  return ($null -ne $json) -and ($null -ne $json['mcpServers']) -and ($null -ne $json['mcpServers']['kin'])
}

$cases = [System.Collections.Generic.List[object]]::new()

function Add-Case([string] $name, [string] $hold, [string] $seed, [scriptblock] $grade) {
  $case = [ordered]@{ name = $name; hold = [ordered]@{ kind = $hold } ; checks = [System.Collections.Generic.List[object]]::new() }
  Write-Seed $seed
  $case.config_before_sha256 = Get-Sha256Hex $claudeConfig
  $case.hold.probe_delete_before = [KinProofNative]::ProbeDeleteOpen($profileRoot)

  $holder = $null
  $exclusive = $null
  try {
    switch ($hold) {
      'none' { }
      'shell-current-directory' {
        $shell = (Get-Command pwsh).Source
        $holder = Start-Process -FilePath $shell -PassThru -WindowStyle Hidden -WorkingDirectory $profileRoot `
          -ArgumentList @('-NoLogo', '-NoProfile', '-Command', 'Start-Sleep -Seconds 900')
        $case.hold.holder_pid = $holder.Id
        $deadline = (Get-Date).AddSeconds(30)
        do {
          Start-Sleep -Milliseconds 250
          $probe = [KinProofNative]::ProbeDeleteOpen($profileRoot)
        } while ($probe -ne 32 -and (Get-Date) -lt $deadline)
      }
      'exclusive' {
        $openError = 0
        $exclusive = [KinProofNative]::OpenExclusive($profileRoot, [ref] $openError)
        if ($exclusive.IsInvalid) {
          $case.hold.error = "could not hold the profile root exclusively (win32 $openError)"
          $exclusive = $null
        }
      }
    }
    $case.hold.probe_delete_during = [KinProofNative]::ProbeDeleteOpen($profileRoot)
    $case.hold.probe_read_during = [KinProofNative]::ProbeReadOpen($profileRoot)
    # A case whose hold could not be taken proves nothing, so setup is not run.
    $run = if ($case.hold.Contains('error')) { [ordered]@{ exit_code = $null; stdout = ''; stderr = '' } } else { Invoke-Setup $name }
  } finally {
    if ($null -ne $exclusive) { $exclusive.Dispose() }
    if ($null -ne $holder) { Stop-Process -Id $holder.Id -Force -ErrorAction SilentlyContinue; $holder.WaitForExit(30000) | Out-Null }
  }
  Start-Sleep -Milliseconds 500
  $case.hold.probe_delete_after = [KinProofNative]::ProbeDeleteOpen($profileRoot)

  $after = Read-Config
  $run.stdout = [string] $run.stdout
  $run.stderr = [string] $run.stderr
  $case.exit_code = $run.exit_code
  $case.config_after_sha256 = $after.sha256
  $case.config_byte_identical = ($case.config_before_sha256 -eq $after.sha256)
  $case.user_content_preserved = if ($seed -eq $unmergeableSeed) { $case.config_byte_identical } else { Test-UserContent $after.json }
  $case.kin_entry_present = Test-KinEntry $after.json
  $case.names_claude_code_as_unconfigured = $run.stderr.Contains('kin setup could not configure Claude Code')
  $case.reports_claude_code_configuration_failed = ($run.stdout + $run.stderr).Contains('Claude Code configuration failed')
  $case.finished_health_checklist = $run.stdout.Contains('=== Health checklist ===')
  $failLine = ((($run.stdout + "`n" + $run.stderr) -split "`r?`n") | Where-Object { $_ -match 'Claude Code configuration failed|could not configure Claude Code' } | Select-Object -First 2) -join ' | '
  $case.failure_lines = $failLine
  $case.stderr_tail = if ($run.stderr.Length -gt 1500) { $run.stderr.Substring($run.stderr.Length - 1500) } else { $run.stderr }

  # The hold must have been real and must be gone, whatever the expectation.
  switch ($hold) {
    'none' {
      $case.checks.Add([ordered]@{ name = 'nothing held the profile root (DELETE open granted before and during)'; pass = ($case.hold.probe_delete_before -eq 0 -and $case.hold.probe_delete_during -eq 0) })
    }
    'shell-current-directory' {
      $case.checks.Add([ordered]@{ name = 'a shell sitting in the profile root refused a DELETE open of it (sharing violation, 32)'; pass = ($case.hold.probe_delete_during -eq 32) })
      $case.checks.Add([ordered]@{ name = 'that shell still allowed a read-only open of the profile root'; pass = ($case.hold.probe_read_during -eq 0) })
    }
    'exclusive' {
      $case.checks.Add([ordered]@{ name = 'the exclusive hold was taken'; pass = (-not $case.hold.Contains('error')) })
      $case.checks.Add([ordered]@{ name = 'the exclusive hold refused even a read-only open of the profile root (32)'; pass = ($case.hold.probe_read_during -eq 32) })
    }
  }
  $case.checks.Add([ordered]@{ name = 'the hold was released after setup (DELETE open granted again)'; pass = ($case.hold.probe_delete_after -eq 0) })
  & $grade $case
  $case.contract_met = -not ($case.checks | Where-Object { -not $_.pass })
  $cases.Add($case)
  $mark = if ($case.contract_met) { 'MET' } else { 'NOT MET' }
  Write-Host "[$name] $mark exit=$($case.exit_code) kin_entry=$($case.kin_entry_present) preserved=$($case.user_content_preserved) :: $failLine"
  foreach ($c in $case.checks) { Write-Host ("    [{0}] {1}" -f ($(if ($c.pass) { 'PASS' } else { 'FAIL' })), $c.name) }
}

function Check($case, [string] $name, [bool] $pass) { $case.checks.Add([ordered]@{ name = $name; pass = $pass }) }

# control: both builds must succeed and merge, keeping the user's content.
Add-Case 'control' 'none' $userSeed {
  param($c)
  Check $c 'setup exited 0' ($c.exit_code -eq 0)
  Check $c "Kin's entry was merged into Claude Code's config" $c.kin_entry_present
  Check $c "the user's existing Claude Code config survived" $c.user_content_preserved
}

if ($Expect -eq 'fixed') {
  Add-Case 'shell-in-profile' 'shell-current-directory' $userSeed {
    param($c)
    Check $c 'setup exited 0 with a shell sitting in the profile root' ($c.exit_code -eq 0)
    Check $c "Kin's entry was still merged into Claude Code's config" $c.kin_entry_present
    Check $c "the user's existing Claude Code config survived" $c.user_content_preserved
  }
  Add-Case 'unmergeable-config' 'none' $unmergeableSeed {
    param($c)
    Check $c 'setup exited 1' ($c.exit_code -eq 1)
    Check $c 'stderr names Claude Code as a client it could not configure' $c.names_claude_code_as_unconfigured
    Check $c 'the unmergeable config was left byte-identical' $c.config_byte_identical
    Check $c 'the rest of setup still ran (health checklist printed)' $c.finished_health_checklist
  }
  Add-Case 'exclusive-hold' 'exclusive' $userSeed {
    param($c)
    Check $c 'setup exited 1' ($c.exit_code -eq 1)
    Check $c 'stderr names Claude Code as a client it could not configure' $c.names_claude_code_as_unconfigured
    Check $c "the user's existing Claude Code config was left byte-identical" $c.config_byte_identical
  }
} else {
  Add-Case 'shell-in-profile' 'shell-current-directory' $userSeed {
    param($c)
    Check $c 'old behaviour: setup exited 0 anyway' ($c.exit_code -eq 0)
    Check $c "old behaviour: Claude Code's configuration failed and Kin's entry was not written" ($c.reports_claude_code_configuration_failed -and -not $c.kin_entry_present)
    Check $c "the user's existing Claude Code config survived" $c.user_content_preserved
  }
  Add-Case 'unmergeable-config' 'none' $unmergeableSeed {
    param($c)
    Check $c 'old behaviour: setup exited 0 anyway' ($c.exit_code -eq 0)
    Check $c "old behaviour: Claude Code's configuration failed line was printed" $c.reports_claude_code_configuration_failed
    Check $c 'the unmergeable config was left byte-identical' $c.config_byte_identical
  }
  Add-Case 'exclusive-hold' 'exclusive' $userSeed {
    param($c)
    Check $c 'old behaviour: setup exited 0 anyway' ($c.exit_code -eq 0)
    Check $c "old behaviour: Claude Code's configuration failed and Kin's entry was not written" ($c.reports_claude_code_configuration_failed -and -not $c.kin_entry_present)
    Check $c "the user's existing Claude Code config survived" $c.user_content_preserved
  }
}

$versionOutput = (& $Kin --version 2>&1 | Out-String).Trim()
$receipt = [ordered]@{
  schema = 'kin-windows-setup-contract/1'
  leg = 'windows-setup-contract'
  kin_version_requested = $KinVersion
  expect = $Expect
  level = 'setup_client_failure_contract'
  level_meaning = if ($Expect -eq 'fixed') {
    "kin setup on native Windows: a shell sitting in the profile root no longer blocks Claude Code's config; a detected client that genuinely cannot be written makes setup exit 1 naming it, with its existing config left exactly as it was; and with nothing blocking, setup succeeds and merges. Graded apart from the MCP handshake. The Claude Code app is not installed; only the config file kin setup writes is under test."
  } else {
    "Negative control for builds before the fix: a blocked Claude Code write is one 'configuration failed' line and setup still exits 0. Graded apart from the MCP handshake. The Claude Code app is not installed; only the config file kin setup writes is under test."
  }
  recorded_at = (Get-Date).ToUniversalTime().ToString('o')
  host = [ordered]@{ os = (Get-CimInstance Win32_OperatingSystem).Caption; build = [Environment]::OSVersion.Version.ToString(); image = "$env:ImageOS $env:ImageVersion"; profile_root = $profileRoot }
  source = if ($InstallJson -and (Test-Path -LiteralPath $InstallJson)) { (Get-Content -Raw -LiteralPath $InstallJson | ConvertFrom-Json).source } else { $null }
  binary = [ordered]@{ path = $Kin; sha256 = Get-Sha256Hex $Kin; version_output = $versionOutput }
  cases = $cases
}
$receipt.version_matches = $versionOutput -match [regex]::Escape($KinVersion)
$receipt.contract_met = ($receipt.version_matches) -and -not ($cases | Where-Object { -not $_.contract_met })
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Out) | Out-Null
$receipt | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8NoBOM -LiteralPath $Out
Write-Host "receipt: $Out"
Write-Host "expect=$Expect version_matches=$($receipt.version_matches) contract_met=$($receipt.contract_met)"
if (-not $receipt.contract_met) { exit 1 }
