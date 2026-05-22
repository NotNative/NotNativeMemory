# Tests for install_lib/manifest_merge.ps1.
#
# Covers the scenarios the uninstaller depends on:
#   - Client-only re-install on top of full keeps install_mode=full and
#     does not drop the database/docker components.
#   - Client-on-server preserves server and adds hooks (already there).
#   - Server-on-full keeps full.
#   - Full-on-client upgrades to full.
#   - No existing manifest returns the new values verbatim.
#   - Components are de-duplicated.
#
# Usage:
#   pwsh -File tests/test_manifest_merge_windows.ps1
#   powershell -File tests/test_manifest_merge_windows.ps1

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
. (Join-Path $root "install_lib\manifest_merge.ps1")

$failed = 0
$total = 0

function Check($label, $cond) {
    $script:total++
    if ($cond) {
        Write-Host "  PASS  $label"
    } else {
        Write-Host "  FAIL  $label"
        $script:failed++
    }
}

function FakeManifest($mode, $components) {
    return [pscustomobject]@{
        install_mode = $mode
        components   = $components
    }
}

# 1. No existing manifest -> passthrough.
$r = Merge-InstallManifest -Existing $null -NewMode "full" -NewComponents @("hooks", "server", "docker", "embedding_model", "database")
Check "null existing keeps new mode" ($r.mode -eq "full")
Check "null existing keeps new components" (($r.components -join ",") -eq "database,docker,embedding_model,hooks,server")

# 2. Client on top of full -> the original bug. Mode must stay full,
#    components must still contain database/docker.
$full = FakeManifest "full" @("hooks", "server", "docker", "embedding_model", "database")
$r = Merge-InstallManifest -Existing $full -NewMode "client" -NewComponents @("hooks")
Check "client-on-full preserves full mode" ($r.mode -eq "full")
Check "client-on-full keeps database component" ($r.components -contains "database")
Check "client-on-full keeps docker component" ($r.components -contains "docker")
Check "client-on-full keeps server component" ($r.components -contains "server")
Check "client-on-full keeps hooks component" ($r.components -contains "hooks")

# 3. Client on top of server -> mode stays server.
$server = FakeManifest "server" @("hooks", "server", "docker", "embedding_model")
$r = Merge-InstallManifest -Existing $server -NewMode "client" -NewComponents @("hooks")
Check "client-on-server preserves server mode" ($r.mode -eq "server")
Check "client-on-server keeps server component" ($r.components -contains "server")

# 4. Server on top of full -> mode stays full and database survives.
$r = Merge-InstallManifest -Existing $full -NewMode "server" -NewComponents @("hooks", "server", "docker", "embedding_model")
Check "server-on-full preserves full mode" ($r.mode -eq "full")
Check "server-on-full keeps database component" ($r.components -contains "database")

# 5. Full on top of client -> upgrades to full.
$client = FakeManifest "client" @("hooks")
$r = Merge-InstallManifest -Existing $client -NewMode "full" -NewComponents @("hooks", "server", "docker", "embedding_model", "database")
Check "full-on-client upgrades to full" ($r.mode -eq "full")
Check "full-on-client has database" ($r.components -contains "database")

# 6. Components are de-duplicated.
$r = Merge-InstallManifest -Existing (FakeManifest "client" @("hooks", "hooks")) -NewMode "client" -NewComponents @("hooks")
Check "duplicate hooks collapse to one" (@($r.components | Where-Object { $_ -eq "hooks" }).Count -eq 1)

# 7. Unknown existing mode falls back to new mode (defensive).
$weird = FakeManifest "garbage" @("hooks")
$r = Merge-InstallManifest -Existing $weird -NewMode "client" -NewComponents @("hooks")
Check "unknown existing mode yields new mode" ($r.mode -eq "client")

Write-Host ""
Write-Host "Tests: $total  Failed: $failed"
if ($failed -gt 0) { exit 1 } else { exit 0 }
