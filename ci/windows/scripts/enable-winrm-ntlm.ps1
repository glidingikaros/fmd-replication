
# WinRM for Ansible in the x64 CI base, keeping the service's encrypted defaults
# (Negotiate/NTLM with message encryption; no Basic, no unencrypted traffic).
# Otherwise mirrors tools/base-image/windows11-arm64/scripts/enable-winrm.ps1.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Set-Service -Name WinRM -StartupType Automatic
if ((Get-Service -Name WinRM).Status -ne 'Running') {
    Start-Service -Name WinRM
}

if (-not @(Get-ChildItem WSMan:\localhost\Listener | Where-Object { $_.Keys -contains 'Transport=HTTP' })) {
    New-Item -Path WSMan:\localhost\Listener -Transport HTTP -Address * -Force | Out-Null
}
Set-Item WSMan:\localhost\Shell\MaxMemoryPerShellMB 1024
Set-Item WSMan:\localhost\MaxTimeoutms 1800000

# As in the paper's base: the vagrant account is a local administrator, which
# needs an unfiltered token over WinRM to run the provisioning playbooks.
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' `
    -Name 'LocalAccountTokenFilterPolicy' -Type DWord -Value 1

if (-not (Get-NetFirewallRule -DisplayName 'FMD WinRM HTTP 5985' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'FMD WinRM HTTP 5985' -Name 'FMD-WinRM-HTTP-5985' `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5985 `
        -Profile Any -Enable True | Out-Null
}

Write-Output 'enable-winrm-ntlm.ps1: WinRM over HTTP/5985 with encrypted Negotiate/NTLM is configured.'
