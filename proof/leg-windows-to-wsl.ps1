# A Windows-side client reaching Kin inside WSL through wsl.exe.
#
# The docs do not describe this path, so the workflow records it on its own and
# does not let it decide the job. The client is still the official MCP SDK
# client, launched on Windows, and the server is the kin installed inside WSL.
# The WSL install record is copied out first so this receipt carries the same
# source and transfer as the in-WSL leg.

param(
  [Parameter(Mandatory)] [string] $Distro,
  [Parameter(Mandatory)] [string] $User,
  [Parameter(Mandatory)] [string] $KinVersion,
  [Parameter(Mandatory)] [string] $InstallMode,
  [string] $Receipts = 'receipts'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$tmp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() }
$homeInWsl = "/home/$User"
$kin = "$homeInWsl/.kin/bin/kin"
New-Item -ItemType Directory -Force -Path $Receipts | Out-Null

$launch = @('-d', $Distro, '-u', $User, '--cd', "$homeInWsl/kin-fixture", '--', $kin, 'mcp', 'start')
$version = @('wsl.exe', '-d', $Distro, '-u', $User, '--', $kin, '--version')
ConvertTo-Json -InputObject $launch -Compress | Set-Content -Encoding utf8NoBOM (Join-Path $tmp 'launch.json')
ConvertTo-Json -InputObject $version -Compress | Set-Content -Encoding utf8NoBOM (Join-Path $tmp 'version.json')

$installJson = Join-Path $tmp 'wsl-install.json'
$raw = (& wsl.exe -d $Distro -u $User -- cat "$homeInWsl/install.json") -join "`n"
Set-Content -Encoding utf8NoBOM -Path $installJson -Value $raw

node (Join-Path $PSScriptRoot 'mcp-proof.mjs') `
  --leg windows-client-to-wsl2 --install-mode $InstallMode --kin-version $KinVersion `
  --repo (Get-Location).Path `
  --launch explicit --command wsl.exe --args-json "@$(Join-Path $tmp 'launch.json')" `
  --version-command-json "@$(Join-Path $tmp 'version.json')" `
  --install-json $installJson `
  --out (Join-Path $Receipts "windows-client-to-wsl2-$InstallMode.json")
$verdict = $LASTEXITCODE
& wsl.exe -d $Distro -u $User -- $kin daemon stop --all --json | Out-Null
exit $verdict
