$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = if ($env:PC_BRIDGE_PORT) { [int]$env:PC_BRIDGE_PORT } else { 19999 }
$webuiPort = if ($env:AGENT_WEBUI_PORT) { [int]$env:AGENT_WEBUI_PORT } else { 8765 }

$adbExe = Join-Path $scriptDir "adb.exe"
if (-not (Test-Path -LiteralPath $adbExe)) {
    $adbInPath = Get-Command "adb.exe" -ErrorAction SilentlyContinue
    if ($adbInPath) {
        $adbExe = $adbInPath.Source
    }
}

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  Phone Agent Bridge - Zero-Login Classroom PC USB Limb (v1.0.0)" -ForegroundColor Cyan
Write-Host "  Compatible: Operit | DSH-Go | Termux | Claude Code | Antigravity" -ForegroundColor DarkCyan
Write-Host "==================================================================" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $adbExe)) {
    Write-Host "[!] adb.exe not found in current folder or system PATH!" -ForegroundColor Red
    Write-Host "    Fix A: Download Classroom_USB_Kit_v1.0.0.zip (bundles official adb.exe) from:" -ForegroundColor Yellow
    Write-Host "           https://github.com/zou-handy/phone-agent-bridge/releases/latest" -ForegroundColor Yellow
    Write-Host "    Fix B: Copy adb.exe, AdbWinApi.dll, AdbWinUsbApi.dll from Google platform-tools here." -ForegroundColor Yellow
    Pause
    exit 1
}

Write-Host "[1/2] Waiting for phone USB debugging (Tap Allow USB Debugging on phone if prompted)..." -ForegroundColor Yellow
& $adbExe wait-for-device
& $adbExe reverse "tcp:$port" "tcp:$port" | Out-Null
& $adbExe forward "tcp:$webuiPort" "tcp:$webuiPort" 2>$null | Out-Null
Write-Host ("[OK] USB reverse tunnel active: Phone 127.0.0.1:" + $port + " -> PC 127.0.0.1:" + $port) -ForegroundColor Green
Write-Host ("[OK] Optional stateless WebUI forward: http://127.0.0.1:" + $webuiPort + "/ (zero login residue on PC)") -ForegroundColor Cyan

$watchdogJob = Start-Job -ScriptBlock {
    param($adbPath, $bridgePort, $uiPort)
    while ($true) {
        try {
            & $adbPath wait-for-device 2>$null
            & $adbPath reverse ("tcp:" + $bridgePort) ("tcp:" + $bridgePort) 2>$null | Out-Null
            & $adbPath forward ("tcp:" + $uiPort) ("tcp:" + $uiPort) 2>$null | Out-Null
        } catch {}
        Start-Sleep -Seconds 2
    }
} -ArgumentList $adbExe, $port, $webuiPort

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add(("http://127.0.0.1:" + $port + "/"))
$listener.Start()

$defaultRoot = if (Test-Path -LiteralPath "D:\") { "D:\" } else { $env:USERPROFILE }
Write-Host ("[2/2] Bridge Server READY on http://127.0.0.1:" + $port + "/ (default cwd: " + $defaultRoot + ")") -ForegroundColor Green
Write-Host "      Unplug/replug USB anytime (2s auto-heal). Close window or unplug when class ends." -ForegroundColor Gray
Write-Host "------------------------------------------------------------------"

try {
    while ($listener.IsListening) {
        $context = $listener.GetContext()
        $req = $context.Request
        $resp = $context.Response
        $route = $req.Url.AbsolutePath

        if ($route -eq "/pull") {
            try {
                $srcPath = $req.QueryString["path"]
                if (-not $srcPath) {
                    $sr = New-Object System.IO.StreamReader($req.InputStream, [System.Text.Encoding]::UTF8)
                    $pullBody = $sr.ReadToEnd()
                    $sr.Close()
                    if ($pullBody) { $srcPath = ($pullBody | ConvertFrom-Json).path }
                }
                if (-not $srcPath -or -not ([System.IO.File]::Exists($srcPath))) {
                    $resp.StatusCode = 404
                    $errJson = [System.Text.Encoding]::UTF8.GetBytes((@{ ok = $false; stderr = ("File not found: " + $srcPath); exit_code = 1 } | ConvertTo-Json -Compress))
                    $resp.ContentType = "application/json; charset=utf-8"
                    $resp.ContentLength64 = $errJson.Length
                    $resp.OutputStream.Write($errJson, 0, $errJson.Length)
                    $resp.Close()
                    continue
                }
                $fileBytes = [System.IO.File]::ReadAllBytes($srcPath)
                Write-Host ("[Agent Pull] " + $srcPath + " (" + $fileBytes.Length + " bytes)") -ForegroundColor DarkGreen
                $resp.StatusCode = 200
                $resp.ContentType = "application/octet-stream"
                $resp.ContentLength64 = $fileBytes.Length
                $resp.OutputStream.Write($fileBytes, 0, $fileBytes.Length)
                $resp.Close()
            } catch {
                $resp.StatusCode = 500
                $errJson = [System.Text.Encoding]::UTF8.GetBytes((@{ ok = $false; stderr = $_.Exception.Message; exit_code = 1 } | ConvertTo-Json -Compress))
                $resp.ContentType = "application/json; charset=utf-8"
                $resp.ContentLength64 = $errJson.Length
                $resp.OutputStream.Write($errJson, 0, $errJson.Length)
                $resp.Close()
            }
            continue
        }

        if ($route -eq "/push") {
            $resultObj = @{ ok = $true; bytes = 0; path = ""; stderr = ""; exit_code = 0 }
            try {
                $dstPath = $null
                $b64Header = $req.Headers["X-Target-Path-B64"]
                if ($b64Header) {
                    $dstPath = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b64Header))
                } else {
                    $dstPath = $req.QueryString["path"]
                }
                if (-not $dstPath) { throw "Missing target path header X-Target-Path-B64 or ?path=" }
                $ms = New-Object System.IO.MemoryStream
                $req.InputStream.CopyTo($ms)
                $rawBytes = $ms.ToArray()
                $ms.Close()
                $parentDir = [System.IO.Path]::GetDirectoryName($dstPath)
                if ($parentDir -and -not [System.IO.Directory]::Exists($parentDir)) {
                    [System.IO.Directory]::CreateDirectory($parentDir) | Out-Null
                }
                [System.IO.File]::WriteAllBytes($dstPath, $rawBytes)
                $resultObj.bytes = $rawBytes.Length
                $resultObj.path = $dstPath
                Write-Host ("[Agent Push] " + $dstPath + " (" + $rawBytes.Length + " bytes, zero-BOM)") -ForegroundColor DarkCyan
            } catch {
                $resultObj.ok = $false
                $resultObj.stderr = $_.Exception.ToString()
                $resultObj.exit_code = 1
            }
            $jsonBytes = [System.Text.Encoding]::UTF8.GetBytes(($resultObj | ConvertTo-Json -Compress -Depth 4))
            $resp.ContentType = "application/json; charset=utf-8"
            $resp.ContentLength64 = $jsonBytes.Length
            $resp.OutputStream.Write($jsonBytes, 0, $jsonBytes.Length)
            $resp.Close()
            continue
        }

        $reader = New-Object System.IO.StreamReader($req.InputStream, [System.Text.Encoding]::UTF8)
        $body = $reader.ReadToEnd()
        $reader.Close()

        $resultObj = @{ ok = $true; stdout = ""; stderr = ""; exit_code = 0; cwd = $defaultRoot }
        try {
            if ($route -eq "/ping") {
                $resultObj.stdout = "pong from " + $env:COMPUTERNAME
            } else {
                $payload = $body | ConvertFrom-Json
                $cmd = $payload.cmd
                $cwd = if ($payload.cwd) { $payload.cwd } else { $defaultRoot }
                if (-not (Test-Path -LiteralPath $cwd)) { $cwd = $defaultRoot }
                $resultObj.cwd = $cwd
                $preview = if ($payload.preview) { $payload.preview } else { $cmd }
                Write-Host ("[Agent Exec] (" + $cwd + ") > " + $preview) -ForegroundColor Yellow

                $escapedCwd = $cwd.Replace("'", "''")
                $runner = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; `$OutputEncoding = [System.Text.Encoding]::UTF8; Set-Location -LiteralPath '" + $escapedCwd + "'; " + $cmd
                $encoded = [System.Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($runner))

                $psi = New-Object System.Diagnostics.ProcessStartInfo
                $psi.FileName = "powershell.exe"
                $psi.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + $encoded
                $psi.RedirectStandardOutput = $true
                $psi.RedirectStandardError = $true
                $psi.UseShellExecute = $false
                $psi.CreateNoWindow = $true
                $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
                $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

                $proc = [System.Diagnostics.Process]::Start($psi)
                $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
                $stderrTask = $proc.StandardError.ReadToEndAsync()
                $proc.WaitForExit()

                $resultObj.stdout = $stdoutTask.GetAwaiter().GetResult()
                $resultObj.stderr = $stderrTask.GetAwaiter().GetResult()
                $resultObj.exit_code = $proc.ExitCode
            }
        } catch {
            $resultObj.ok = $false
            $resultObj.stderr = $_.Exception.ToString()
            $resultObj.exit_code = 1
        }

        $jsonBytes = [System.Text.Encoding]::UTF8.GetBytes(($resultObj | ConvertTo-Json -Compress -Depth 4))
        $resp.ContentType = "application/json; charset=utf-8"
        $resp.ContentLength64 = $jsonBytes.Length
        $resp.OutputStream.Write($jsonBytes, 0, $jsonBytes.Length)
        $resp.Close()
    }
} finally {
    Stop-Job $watchdogJob -ErrorAction SilentlyContinue
    Remove-Job $watchdogJob -Force -ErrorAction SilentlyContinue
    $listener.Stop()
}
