$ErrorActionPreference = "Stop"
try {
    chcp.com 65001 > $null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$BootstrapUrl = "https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1"
$script = Invoke-WebRequest -UseBasicParsing -Uri $BootstrapUrl
$content = ([string]$script.Content).TrimStart([char]0xFEFF)
& ([scriptblock]::Create($content)) -Clean
