# Firewall configuration helpers for install_windows.ps1.
#
# Two functions:
#   Get-FirewallProfileList   -- pure: maps a boolean to the comma-joined
#                                 -Profile string for New-NetFirewallRule.
#   Add-NnmFirewallRuleIfMissing -- side-effecting: looks up the rule by
#                                 DisplayName; if absent, creates it. Idempotent.
#
# The installer wraps these with Read-Host prompts; tests dot-source this
# file directly and call the functions with explicit parameters.
#
# Style: ASCII-only output to survive legacy Windows console codepage.

function Get-FirewallProfileList {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [bool]$IncludePublic
    )

    # Always cover Domain + Private. Domain = the machine is joined to a
    # Windows domain and the active network is that domain network;
    # Private = a trusted home/office LAN. Both are reasonable defaults
    # for an MCP server you want to reach from your own LAN.
    #
    # Public is opt-in because that profile applies to coffee-shop wifi,
    # hotel networks, etc -- anywhere you would not want random people
    # on the same SSID hitting your memory store.
    if ($IncludePublic) {
        return "Domain,Private,Public"
    }
    return "Domain,Private"
}

function Get-NnmFirewallRuleName {
    param([int]$Port)
    return "NotNativeMemory MCP $Port"
}

function Add-NnmFirewallRuleIfMissing {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port,

        [Parameter(Mandatory = $true)]
        [string]$Profiles,

        # When true, emit the command that would run instead of running
        # it. Used by tests so they do not need admin and do not leave
        # firewall rules behind.
        [bool]$WhatIf = $false
    )

    $ruleName = Get-NnmFirewallRuleName -Port $Port

    # Existence check is idempotent: re-running the installer on a host
    # that already has the rule leaves it alone. The user's stated rule
    # is "check if it exists, skip if it does" -- never replace.
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if ($existing) {
        return @{ action = "skipped-exists"; rule = $ruleName }
    }

    if ($WhatIf) {
        return @{
            action  = "would-create"
            rule    = $ruleName
            port    = $Port
            profile = $Profiles
        }
    }

    try {
        New-NetFirewallRule `
            -DisplayName $ruleName `
            -Direction Inbound `
            -Protocol TCP `
            -LocalPort $Port `
            -Action Allow `
            -Profile $Profiles `
            -ErrorAction Stop | Out-Null
        return @{ action = "created"; rule = $ruleName; profile = $Profiles }
    } catch {
        # The most common failure here is "not elevated". Surface a
        # clean dict to the caller so the installer can print a useful
        # message instead of a raw stack trace.
        return @{
            action = "failed"
            rule   = $ruleName
            error  = $_.Exception.Message
        }
    }
}
