# Enable Remote Desktop so the console's in-browser session can reach this guest.
#
# Runs at firstboot rather than as an executor step for three reasons: it must
# work on a VM whose agent never came up (which is exactly when an operator most
# needs to look inside), the agent's command catalog has no RDP command, and
# there is nothing role-specific to wait for. Numbered 30- so the static address
# from 20-network is already live and the (slower) agent install still follows.
#
# Does not reboot — the base image's firstboot runner owns the single reboot.
# Terminal Services picks up fDenyTSConnections without one anyway.

$ErrorActionPreference = 'Stop'

$server = 'HKLM:\System\CurrentControlSet\Control\Terminal Server'
$rdpTcp = Join-Path $server 'WinStations\RDP-Tcp'

# 0 = accept connections. This is the master switch behind the
# "Allow remote connections to this computer" checkbox.
Set-ItemProperty -Path $server -Name 'fDenyTSConnections' -Value 0 -Type DWord

# Keep Network Level Authentication on: guacd negotiates it, and turning it off
# to make a lab "just work" would leave the pre-auth RDP surface exposed on every
# guest on the shared subnet.
Set-ItemProperty -Path $rdpTcp -Name 'UserAuthentication' -Value 1 -Type DWord

# The rules ship present-but-disabled on a fresh Windows Server image. Match on
# the group rather than a rule name: the names are localized, the group key is not.
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'

Set-Service -Name 'TermService' -StartupType Automatic
if ((Get-Service -Name 'TermService').Status -ne 'Running') {
    Start-Service -Name 'TermService'
}

Write-Host 'Remote Desktop enabled (NLA on, firewall group enabled, TermService automatic).'
