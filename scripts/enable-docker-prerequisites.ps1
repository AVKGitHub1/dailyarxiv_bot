# Run from an elevated PowerShell. Never restarts Windows automatically.
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
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
    $ruleName = 'DailyArxiv-LAN-8765'
    if (-not (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name $ruleName -DisplayName 'Daily arXiv - local network TCP 8765' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -RemoteAddress LocalSubnet -Profile Private,Domain | Out-Null
    }
    $result.Firewall = 'TCP 8765 allowed from local subnets on Private and Domain networks.'
} catch {
    $result.Error = $_.Exception.Message
} finally {
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $resultDirectory 'docker-prerequisites.json') -Encoding UTF8
}
if ($result.Error) { Write-Error $result.Error; exit 1 }
$result | ConvertTo-Json -Depth 4
