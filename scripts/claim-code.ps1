param(
    [switch]$Rotate,
    [string]$HostName = "localhost",
    [int]$Port = 9500
)

$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

if ($HostName -eq "localhost" -and $env:COMPUTERNAME) {
    $HostName = $env:COMPUTERNAME.ToLower()
}

$argsList = @("scripts/claim_code.py", "--host", $HostName, "--port", "$Port")
if ($Rotate) { $argsList += "--rotate" }

docker container inspect MCP-server 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    & docker compose --progress=plain -f docker/docker-compose.yml exec -T mcp python @argsList
    exit $LASTEXITCODE
}

Write-Host "MCP-server container was not found; falling back to host Python."
& python @argsList
exit $LASTEXITCODE
