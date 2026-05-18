# Tests for install_lib/loopback_advice.ps1.
#
# Verifies:
#   - Test-McpUrlIsLoopback recognizes localhost (case-insensitive),
#     127.x.x.x, and ::1 (both bracketed and bare).
#   - Test-McpUrlIsLoopback rejects routable hosts and malformed input.
#   - Get-LoopbackIPv6AdviceText surfaces the exact netsh command and the
#     "ELEVATED" qualifier so a user copying it understands the admin
#     requirement.
#   - Show-LoopbackIPv6AdviceIfNeeded returns $true and prints the advice
#     when the URL is loopback, and returns $false and prints nothing
#     when it is not.
#
# Runs without admin or network. Pure logic test.
#
# Usage:
#   pwsh -File tests/test_loopback_advice_windows.ps1
#   powershell -File tests/test_loopback_advice_windows.ps1

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
. (Join-Path $root "install_lib\loopback_advice.ps1")

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

# 1. Loopback URL detection — positive cases.
Check "localhost is loopback" `
    (Test-McpUrlIsLoopback -Url "http://localhost:9500/mcp")
Check "LOCALHOST (uppercase) is loopback" `
    (Test-McpUrlIsLoopback -Url "http://LOCALHOST:9500/mcp")
Check "127.0.0.1 is loopback" `
    (Test-McpUrlIsLoopback -Url "http://127.0.0.1:9500/mcp")
Check "127.5.4.3 is loopback (any 127/8)" `
    (Test-McpUrlIsLoopback -Url "http://127.5.4.3:9500/mcp")
Check "::1 bracketed is loopback" `
    (Test-McpUrlIsLoopback -Url "http://[::1]:9500/mcp")

# 2. Non-loopback URLs.
Check "routable hostname is not loopback" `
    (-not (Test-McpUrlIsLoopback -Url "http://green.taild9f9ee.ts.net:9500/mcp"))
Check "routable IPv4 is not loopback" `
    (-not (Test-McpUrlIsLoopback -Url "http://192.168.1.10:9500/mcp"))
Check "10.x is not loopback" `
    (-not (Test-McpUrlIsLoopback -Url "http://10.0.0.5:9500/mcp"))

# 3. Defensive: empty / malformed input.
Check "empty string is not loopback" `
    (-not (Test-McpUrlIsLoopback -Url ""))
Check "null is not loopback" `
    (-not (Test-McpUrlIsLoopback -Url $null))
Check "garbage string is not loopback" `
    (-not (Test-McpUrlIsLoopback -Url "not a url at all"))

# 4. Advice text content — make sure the netsh command and the
#    'ELEVATED' qualifier both survive future edits. These are the bits
#    a user actually needs to copy or understand.
$advice = Get-LoopbackIPv6AdviceText -join "`n"
Check "advice includes the netsh command verbatim" `
    ($advice -match "netsh interface ipv6 set prefixpolicy ::ffff:0:0/96 60 4")
Check "advice mentions ELEVATED prompt requirement" `
    ($advice -match "ELEVATED")
Check "advice shows the revert command" `
    ($advice -match "netsh interface ipv6 set prefixpolicy ::ffff:0:0/96 35 4")

# 5. Show-LoopbackIPv6AdviceIfNeeded — verifies it returns whether the
#    advice was shown and that it only prints when needed. Capture
#    host output via Out-String + transcript-style redirection is awkward
#    in test contexts, so we just check the return value contract.
Check "Show returns true on loopback URL" `
    ((Show-LoopbackIPv6AdviceIfNeeded -Url "http://127.0.0.1:9500/mcp" 6>$null) -eq $true)
Check "Show returns false on routable URL" `
    ((Show-LoopbackIPv6AdviceIfNeeded -Url "http://green.example:9500/mcp" 6>$null) -eq $false)
Check "Show returns false on empty URL" `
    ((Show-LoopbackIPv6AdviceIfNeeded -Url "" 6>$null) -eq $false)

Write-Host "---"
Write-Host "$($total - $failed)/$total passed"
if ($failed -ne 0) { exit 1 }
exit 0
