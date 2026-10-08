# First-logon provisioning of the x64 CI base: the paper's base scripts, then WinRM.
# As in the paper's base it runs only from FirstLogonCommands, after Windows Setup and OOBE
# have finished: WinRM answering then means the guest is set up. (An earlier SetupComplete.cmd
# route enabled WinRM minutes before OOBE ended, and the build went on while Setup was unfinished.)
# Every step is written to C:\fmd\first-logon.log and to COM1, which CI keeps.
# -Stage (specialize pass): copy the scripts to C:\fmd\scripts, a fallback source for FirstLogonCommands.
param([string]$Source = $PSScriptRoot, [switch]$Stage)

$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path C:\fmd\scripts | Out-Null

function Say([string]$Text) {
    $line = '{0:HH:mm:ss} {1}' -f (Get-Date), $Text
    Add-Content -Path C:\fmd\first-logon.log -Value $line
    try {
        $port = New-Object System.IO.Ports.SerialPort 'COM1', 115200
        $port.Open()
        $port.WriteLine("FMD $line")
        $port.Close()
    } catch { }
}

Say "first-logon from $Source as $([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
if ((Resolve-Path $Source).Path -ne 'C:\fmd\scripts') {
    foreach ($file in Get-ChildItem -Path $Source -File) {  # canonical names whichever CD name table Windows reads
        $name = ($file.Name -replace ';\d+$', '').ToLowerInvariant().Replace('_', '-')
        Copy-Item -LiteralPath $file.FullName -Destination (Join-Path C:\fmd\scripts $name) -Force
    }
}
if ($Stage) {
    Say 'staged scripts'
    return
}
foreach ($name in 'disable-sleep-hibernate', 'set-network-private', 'disable-update-reboots',
                  'disable-automatic-updates', 'enable-autologon', 'enable-winrm-ntlm') {
    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\fmd\scripts\$name.ps1" 2>&1
    Say ("{0}: exit {1}: {2}" -f $name, $LASTEXITCODE, ($output -join ' | '))
}
Set-Content -Path C:\fmd\first-logon-complete.txt -Value 'first-logon-provisioning-complete'
Say 'first-logon complete'
