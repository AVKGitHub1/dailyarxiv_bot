# Run from an elevated PowerShell. Never restarts Windows automatically.
# Without -Port it uses ARXIV_PORT as Compose does: the environment first, then .env
# beside compose.yaml, else 8765. An elevated session may not inherit your variables.
param([int]$Port = 0)
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
if ($Port -le 0) {
    $configured = $env:ARXIV_PORT
    $envFile = Join-Path $projectDirectory '.env'
    if (-not $configured -and (Test-Path -LiteralPath $envFile)) {
        $line = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*ARXIV_PORT\s*=' } | Select-Object -Last 1
        if ($line) { $configured = ($line -split '=', 2)[1].Trim().Trim('"', "'") }
    }
    $Port = if ($configured) { [int]$configured } else { 8765 }
}
$resultDirectory = Join-Path $projectDirectory 'data'
New-Item -ItemType Directory -Path $resultDirectory -Force | Out-Null
$result = [ordered]@{ Features = @(); RestartNeeded = $false; Firewall = ''; Error = '' }
try {
    foreach ($featureName in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
        $feature = Get-WindowsOptionalFeature -Online -FeatureName $featureName
        if ($feature.State -ne 'Enabled') {
            $enabledFeature = Enable-WindowsOptionalFeature -Online -FeatureName $featureName -All -NoRestart
            $result.RestartNeeded = $result.RestartNeeded -or [bool]$enabledFeature.RestartNeeded
        }
        $currentFeature = Get-WindowsOptionalFeature -Online -FeatureName $featureName
        $result.Features += @{ Name = $featureName; State = [string]$currentFeature.State }
        if ([string]$currentFeature.State -eq 'EnablePending') { $result.RestartNeeded = $true }
    }
    $ruleName = "DailyArxiv-LAN-$Port"
    if (-not (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name $ruleName -DisplayName "Daily arXiv - local network TCP $Port" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Private,Domain | Out-Null
    }
    $result.Firewall = "TCP $Port allowed from local subnets on Private and Domain networks."
} catch {
    $result.Error = $_.Exception.Message
} finally {
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $resultDirectory 'docker-prerequisites.json') -Encoding UTF8
}
if ($result.Error) { Write-Error $result.Error; exit 1 }
$result | ConvertTo-Json -Depth 4
