# Loopback advice helpers for install_windows.ps1.
#
# Windows ships with a prefix-policy table that resolves "localhost"
# preferring IPv6 (::1) over IPv4-mapped (::ffff:127.0.0.1). When the
# MCP server binds only to 127.0.0.1, clients on the same machine that
# resolve "localhost" land on ::1 first and time out before falling
# back to IPv4. The fix is to bump precedence of ::ffff:0:0/96 so IPv4
# wins. It requires an elevated shell because it mutates the system
# prefix-policy table.
#
# We never auto-apply (changes a system-wide network preference and
# needs admin); we only surface the advice when the resolved MCP URL
# is loopback. Detection lives here in its own helper so a PS test
# can exercise it without sourcing the whole installer.


function Test-McpUrlIsLoopback {
    <#
    .SYNOPSIS
        Returns $true when the MCP URL points at the local machine.

    .DESCRIPTION
        Delegates to .NET's Uri.IsLoopback which already covers 'localhost'
        (any case), any 127.x.x.x address, and ::1 (with or without
        brackets, expanded or shorthand). Empty or unparseable input
        returns $false.
    #>
    param([string]$Url)

    if ([string]::IsNullOrWhiteSpace($Url)) { return $false }

    try {
        $uri = [System.Uri]$Url
    } catch {
        return $false
    }

    if ([string]::IsNullOrWhiteSpace($uri.Host)) { return $false }
    return [bool]$uri.IsLoopback
}


function Get-LoopbackIPv6AdviceText {
    <#
    .SYNOPSIS
        Returns the multi-line advice text shown when the MCP URL is loopback.

    .DESCRIPTION
        Pure-string helper so tests can assert content without capturing
        host output. The installer prints this; tests inspect it.
    #>
    return @(
        "",
        "Note: the MCP URL resolves to the local machine.",
        "Windows' default IPv6 prefix-policy may cause clients that resolve",
        "'localhost' to prefer ::1 over 127.0.0.1, which times out when the",
        "MCP server binds only to IPv4. If hook calls hang or 401 with no",
        "server-side hit, run the following in an ELEVATED PowerShell prompt",
        "to make IPv4-mapped addresses win:",
        "",
        "    netsh interface ipv6 set prefixpolicy ::ffff:0:0/96 60 4",
        "",
        "This is a system-wide change. Revert with:",
        "",
        "    netsh interface ipv6 set prefixpolicy ::ffff:0:0/96 35 4"
    )
}


function Show-LoopbackIPv6AdviceIfNeeded {
    <#
    .SYNOPSIS
        Prints the loopback advice text when the URL is loopback. No-op otherwise.
    #>
    param([string]$Url)

    if (-not (Test-McpUrlIsLoopback -Url $Url)) { return $false }
    foreach ($line in Get-LoopbackIPv6AdviceText) {
        Write-Host $line
    }
    return $true
}
