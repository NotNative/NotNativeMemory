# Tests for install_lib/firewall.ps1.
#
# Verifies:
#   - Get-FirewallProfileList maps IncludePublic to the right comma-joined string
#   - Get-NnmFirewallRuleName returns a stable, port-keyed identifier
#   - Add-NnmFirewallRuleIfMissing with -WhatIf does not touch the firewall
#     and reports the intended action plus parameters
#   - Add-NnmFirewallRuleIfMissing returns 'skipped-exists' when a rule with
#     the same DisplayName is already present (mocked here so the test does
#     not need admin)
#
# Runs without admin; uses Mock to stub Get-NetFirewallRule / New-NetFirewallRule
# so the test never modifies real firewall state.
#
# Usage:
#   pwsh -File tests/test_firewall_windows.ps1
#   powershell -File tests/test_firewall_windows.ps1

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
. (Join-Path $root "install_lib\firewall.ps1")

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

# 1. Profile list mapping.
Check "Get-FirewallProfileList -IncludePublic `$false returns Domain,Private" `
    ((Get-FirewallProfileList -IncludePublic $false) -eq "Domain,Private")
Check "Get-FirewallProfileList -IncludePublic `$true adds Public" `
    ((Get-FirewallProfileList -IncludePublic $true) -eq "Domain,Private,Public")

# 2. Stable rule name.
Check "rule name is port-keyed" `
    ((Get-NnmFirewallRuleName -Port 9500) -eq "NotNativeMemory MCP 9500")
Check "rule name differs across ports" `
    ((Get-NnmFirewallRuleName -Port 9500) -ne (Get-NnmFirewallRuleName -Port 9501))

# 3. WhatIf path: monkey-patch Get-NetFirewallRule to return $null so the
#    "exists?" branch falls through to the would-create branch.
function Get-NetFirewallRule {
    param([string]$DisplayName, $ErrorAction)
    return $null
}

$result = Add-NnmFirewallRuleIfMissing -Port 9500 -Profiles "Domain,Private" -WhatIf $true
Check "WhatIf reports would-create" ($result.action -eq "would-create")
Check "WhatIf preserves port" ($result.port -eq 9500)
Check "WhatIf preserves profiles" ($result.profile -eq "Domain,Private")
Check "WhatIf surfaces the rule name" ($result.rule -eq "NotNativeMemory MCP 9500")

# 4. Existing-rule path: monkey-patch Get-NetFirewallRule to return a stub
#    so the function takes the skip branch.
function Get-NetFirewallRule {
    param([string]$DisplayName, $ErrorAction)
    return [PSCustomObject]@{ DisplayName = $DisplayName }
}

$result = Add-NnmFirewallRuleIfMissing -Port 9500 -Profiles "Domain,Private" -WhatIf $false
Check "skipped-exists when rule already present" `
    ($result.action -eq "skipped-exists")
Check "skipped-exists reports the rule name" `
    ($result.rule -eq "NotNativeMemory MCP 9500")

Write-Host "---"
Write-Host "$($total - $failed)/$total passed"
if ($failed -ne 0) { exit 1 }
exit 0
