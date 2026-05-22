# Merge a new install pass into an existing manifest so re-running the
# installer with a lighter mode (e.g. 3-client on top of 1-full) does
# not erase what was actually installed. The uninstaller reads the
# manifest verbatim, so a stomped manifest = orphaned containers.
#
# Rules:
#   install_mode -> the heavier of (existing, new) wins.
#                   ranking: full(3) > server(2) > client(1)
#   components   -> set union of both lists.
#
# Returns a hashtable { mode = <string>; components = <string[]> }.
# When $Existing is $null, returns the new values unchanged.

function Merge-InstallManifest {
    param(
        $Existing,
        [string]$NewMode,
        [string[]]$NewComponents
    )

    if (-not $Existing) {
        $sorted = @($NewComponents) | Where-Object { $_ } | Sort-Object -Unique
        return @{ mode = $NewMode; components = @($sorted) }
    }

    $rank = @{ "client" = 1; "server" = 2; "full" = 3 }
    $oldMode = [string]$Existing.install_mode
    $oldRank = if ($rank.ContainsKey($oldMode)) { $rank[$oldMode] } else { 0 }
    $newRank = if ($rank.ContainsKey($NewMode)) { $rank[$NewMode] } else { 0 }
    $finalMode = if ($oldRank -gt $newRank) { $oldMode } else { $NewMode }

    $oldComponents = @()
    if ($Existing.components) { $oldComponents = @($Existing.components) }
    $merged = @($oldComponents + $NewComponents) | Where-Object { $_ } | Sort-Object -Unique

    return @{ mode = $finalMode; components = @($merged) }
}
