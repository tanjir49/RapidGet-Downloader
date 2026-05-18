$ErrorActionPreference = "SilentlyContinue"
$extensionPath = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Clipboard $extensionPath

$browsers = @(
  @{ Name = "Chrome"; Path = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"; Url = "chrome://extensions/" },
  @{ Name = "Chrome"; Path = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"; Url = "chrome://extensions/" },
  @{ Name = "Edge"; Path = "$env:ProgramFiles(x86)\Microsoft\Edge\Application\msedge.exe"; Url = "edge://extensions/" },
  @{ Name = "Edge"; Path = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"; Url = "edge://extensions/" },
  @{ Name = "Brave"; Path = "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe"; Url = "brave://extensions/" },
  @{ Name = "Firefox"; Path = "$env:ProgramFiles\Mozilla Firefox\firefox.exe"; Url = "about:debugging#/runtime/this-firefox" }
)

$opened = @{}
foreach ($browser in $browsers) {
  if ((Test-Path $browser.Path) -and -not $opened.ContainsKey($browser.Name)) {
    $opened[$browser.Name] = $true
    Start-Process $browser.Path $browser.Url
    Write-Host "Opened $($browser.Name) extension page."
  }
}

Write-Host ""
Write-Host "RapidGet extension folder copied to clipboard:"
Write-Host $extensionPath
Write-Host ""
Write-Host "Chrome/Edge/Brave: enable Developer mode, click Load unpacked, paste this folder path."
Write-Host "Firefox: click This Firefox > Load Temporary Add-on, then select manifest.json from this folder."
Read-Host "Press Enter to close"
