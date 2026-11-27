# Classroom Portal MCP - Zero-Residue Windows Limb (100% Pure ASCII + CRLF)
# Battle-hardened against Seewo VDI pitfalls:
#   1) Pure ASCII only -> immune to Windows CP936 quote/newline corruption on double-click
#   2) UTF-8 Base64 -EncodedCommand + ReadToEndAsync() -> immune to quote traps & .NET 4KB pipe deadlock
#   3) Outbound HTTPS :443 long-poll -> zero Windows Defender Firewall popup, zero UAC, penetrates NAT
#   4) Pure RAM background job table + finally tree-kill -> closing window leaves 0 residue

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$HubUrl = if ($env:PORTAL_HUB_URL) { $env:PORTAL_HUB_URL.TrimEnd("/") } else { "__DEFAULT_HUB_URL__" }
if ($HubUrl -eq ("__" + "DEFAULT_HUB_URL" + "__")) {
    $HubUrl = Read-Host "Enter Classroom Portal Hub URL (e.g. https://portal.example.com)"
    $HubUrl = $HubUrl.Trim().TrimEnd("/")
}

$DefaultCwd = if (Test-Path -LiteralPath "D:\") { "D:\" } else { $env:USERPROFILE }
$BgJobs = @{}

function Test-BlockedSystemPath([string]$TargetPath) {
    if (-not $TargetPath) { return $true }
    try {
        $full = [System.IO.Path]::GetFullPath($TargetPath).ToLowerInvariant()
        if ($full.StartsWith("c:\windows") -or $full.StartsWith("c:\program files")) {
            return $true
        }
    } catch {
        return $true
    }
    return $false
}

function Invoke-HubJson([string]$Method, [string]$Path, [hashtable]$Headers, $BodyObj, [int]$TimeoutSec = 35) {
    $uri = $HubUrl + $Path
    $req = [System.Net.HttpWebRequest]::Create($uri)
    $req.Method = $Method
    $req.Timeout = $TimeoutSec * 1000
    $req.ReadWriteTimeout = $TimeoutSec * 1000
    $req.KeepAlive = $true
    if ($Headers) {
        foreach ($k in $Headers.Keys) {
            $req.Headers[$k] = $Headers[$k]
        }
    }
    if ($null -ne $BodyObj) {
        $jsonStr = $BodyObj | ConvertTo-Json -Compress -Depth 8
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($jsonStr)
        $req.ContentType = "application/json; charset=utf-8"
        $req.ContentLength = $bytes.Length
        $reqStream = $req.GetRequestStream()
        $reqStream.Write($bytes, 0, $bytes.Length)
        $reqStream.Close()
    }
    $resp = $req.GetResponse()
    $sr = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
    $txt = $sr.ReadToEnd()
    $sr.Close()
    $resp.Close()
    if (-not $txt) { return @{} }
    return ($txt | ConvertFrom-Json)
}

function Build-PowerShellEncoded([string]$CmdText, [string]$WorkDir) {
    $safeCwd = $WorkDir.Replace("'", "''")
    $runner = "`$ProgressPreference = 'SilentlyContinue'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; `$OutputEncoding = [System.Text.Encoding]::UTF8; Set-Location -LiteralPath '" + $safeCwd + "'; " + $CmdText
    return [System.Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($runner))
}

function Run-ForegroundCommand($ArgsObj) {
    $cmd = [string]$ArgsObj.command
    $cwd = if ($ArgsObj.cwd -and (Test-Path -LiteralPath ([string]$ArgsObj.cwd))) { [string]$ArgsObj.cwd } else { $DefaultCwd }
    $shell = if ($ArgsObj.shell) { ([string]$ArgsObj.shell).ToLowerInvariant() } else { "powershell" }
    $timeoutSec = if ($ArgsObj.timeout_sec) { [int]$ArgsObj.timeout_sec } else { 120 }
    if ($timeoutSec -lt 1) { $timeoutSec = 1 }
    if ($timeoutSec -gt 600) { $timeoutSec = 600 }

    Write-Host ("[RUN] (" + $cwd + ") > " + $cmd) -ForegroundColor Yellow

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    if ($shell -eq "cmd") {
        $psi.FileName = "cmd.exe"
        $psi.Arguments = "/d /s /c chcp 65001 >nul & " + $cmd
        $psi.WorkingDirectory = $cwd
    } else {
        $exe = if ($shell -eq "pwsh" -and (Get-Command "pwsh.exe" -ErrorAction SilentlyContinue)) { "pwsh.exe" } else { "powershell.exe" }
        $psi.FileName = $exe
        $psi.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + (Build-PowerShellEncoded $cmd $cwd)
    }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $proc = [System.Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()

    $finished = $proc.WaitForExit($timeoutSec * 1000)
    if (-not $finished) {
        & taskkill.exe /T /F /PID $proc.Id 2>$null | Out-Null
        return @{
            ok = $false
            timed_out = $true
            exit_code = -1
            cwd = $cwd
            stdout = ""
            stderr = ("Command timed out after " + $timeoutSec + "s (tree killed).")
        }
    }
    $stdout = $outTask.GetAwaiter().GetResult()
    $stderr = $errTask.GetAwaiter().GetResult()
    return @{
        ok = ($proc.ExitCode -eq 0)
        exit_code = $proc.ExitCode
        cwd = $cwd
        stdout = $stdout
        stderr = $stderr
    }
}

function Start-BackgroundJob($ArgsObj) {
    $activeCount = 0
    foreach ($j in $BgJobs.Values) {
        if (-not $j.Proc.HasExited) { $activeCount += 1 }
    }
    if ($activeCount -ge 4) {
        return @{ ok = $false; error = "Max 4 concurrent background commands reached. Call stop_command first." }
    }
    $cmd = [string]$ArgsObj.command
    $cwd = if ($ArgsObj.cwd -and (Test-Path -LiteralPath ([string]$ArgsObj.cwd))) { [string]$ArgsObj.cwd } else { $DefaultCwd }
    $jobId = "cmd_" + ([guid]::NewGuid().ToString("N").Substring(0, 8))
    $outFile = Join-Path $env:TEMP ("portal_" + $jobId + ".log")

    $wrapped = "& { " + $cmd + " } *>&1 | Out-File -FilePath '" + $outFile.Replace("'", "''") + "' -Encoding utf8"
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "powershell.exe"
    $psi.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + (Build-PowerShellEncoded $wrapped $cwd)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    $BgJobs[$jobId] = @{
        Id = $jobId
        Pid = $proc.Id
        Command = $cmd
        Cwd = $cwd
        Proc = $proc
        LogFile = $outFile
        StartedAt = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    }
    Write-Host ("[BG-START] " + $jobId + " (PID " + $proc.Id + ") > " + $cmd) -ForegroundColor Cyan
    return @{ ok = $true; command_id = $jobId; pid = $proc.Id; cwd = $cwd }
}

function Read-BackgroundJob($ArgsObj) {
    $cid = if ($ArgsObj.command_id) { [string]$ArgsObj.command_id } else { "" }
    if (-not $cid) {
        $list = @()
        foreach ($j in $BgJobs.Values) {
            $running = (-not $j.Proc.HasExited)
            $list += @{
                command_id = $j.Id
                pid = $j.Pid
                running = $running
                exit_code = if ($running) { $null } else { $j.Proc.ExitCode }
                command = $j.Command
            }
        }
        return @{ ok = $true; jobs = $list }
    }
    if (-not $BgJobs.ContainsKey($cid)) {
        return @{ ok = $false; error = ("Unknown command_id: " + $cid) }
    }
    $j = $BgJobs[$cid]
    $offset = if ($ArgsObj.offset) { [int]$ArgsObj.offset } else { 0 }
    if ($offset -lt 0) { $offset = 0 }
    $content = ""
    if (Test-Path -LiteralPath $j.LogFile) {
        try {
            $fs = [System.IO.File]::Open($j.LogFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
            $content = $sr.ReadToEnd()
            $sr.Close()
            $fs.Close()
        } catch {}
    }
    $slice = if ($offset -lt $content.Length) { $content.Substring($offset) } else { "" }
    $running = (-not $j.Proc.HasExited)
    return @{
        ok = $true
        command_id = $cid
        pid = $j.Pid
        running = $running
        exit_code = if ($running) { $null } else { $j.Proc.ExitCode }
        output = $slice
        next_offset = $content.Length
    }
}

function Stop-BackgroundJob($ArgsObj) {
    $cid = [string]$ArgsObj.command_id
    if (-not $BgJobs.ContainsKey($cid)) {
        return @{ ok = $false; error = ("Unknown command_id: " + $cid) }
    }
    $j = $BgJobs[$cid]
    if (-not $j.Proc.HasExited) {
        & taskkill.exe /T /F /PID $j.Pid 2>$null | Out-Null
    }
    if (Test-Path -LiteralPath $j.LogFile) {
        Remove-Item -LiteralPath $j.LogFile -Force -ErrorAction SilentlyContinue
    }
    $BgJobs.Remove($cid)
    Write-Host ("[BG-STOP] " + $cid) -ForegroundColor DarkYellow
    return @{ ok = $true; stopped = $cid }
}

function Resolve-WorkspacePath([string]$RawPath) {
    if ([System.IO.Path]::IsPathRooted($RawPath)) {
        return [System.IO.Path]::GetFullPath($RawPath)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $DefaultCwd $RawPath))
}

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  Classroom Portal MCP - Zero-Residue Temporary Limb (v1.0.0)" -ForegroundColor Cyan
Write-Host "  5 Minimal Tools + OAuth 2.0 + 6-Digit Screen PIN | 0 Account Residue" -ForegroundColor DarkCyan
Write-Host "==================================================================" -ForegroundColor Cyan

$osVer = [System.Environment]::OSVersion.VersionString
$reg = Invoke-HubJson "POST" "/limb/register" @{} @{
    hostname = $env:COMPUTERNAME
    os_info = $osVer
    default_cwd = $DefaultCwd
} 15

if (-not $reg.ok) {
    Write-Host ("[!] Failed to register with Hub: " + $HubUrl) -ForegroundColor Red
    exit 1
}

$LimbSecret = [string]$reg.limb_secret
$CurrentPin = [string]$reg.pin_formatted
$McpUrl = [string]$reg.mcp_endpoint

Write-Host ""
Write-Host ("  [6-DIGIT PAIRING PIN]  >>>  " + $CurrentPin + "  <<<") -ForegroundColor Green
Write-Host ("  [MCP CONNECTOR URL]    " + $McpUrl) -ForegroundColor Gray
Write-Host ("  [DEFAULT WORKSPACE]    " + $DefaultCwd) -ForegroundColor Gray
Write-Host "  Close this window when class ends -> instant self-destruct (0 residue)." -ForegroundColor DarkGray
Write-Host "------------------------------------------------------------------"

try {
    while ($true) {
        try {
            $poll = Invoke-HubJson "POST" "/limb/poll" @{ "X-Limb-Secret" = $LimbSecret } @{} 35
        } catch {
            Start-Sleep -Seconds 2
            continue
        }
        if (-not $poll.ok) {
            Write-Host "[!] Room closed on Hub. Exiting." -ForegroundColor Yellow
            break
        }
        if ($poll.pin_note -and $poll.pin_formatted -ne $CurrentPin) {
            $CurrentPin = [string]$poll.pin_formatted
            Write-Host ("[PIN ROTATED] (" + $poll.pin_note + ") New Next-Client PIN: >>> " + $CurrentPin + " <<<") -ForegroundColor Cyan
        }
        if ($poll.tasks) {
            foreach ($t in $poll.tasks) {
                $tid = [string]$t.task_id
                $op = [string]$t.op
                $pl = $t.payload
                $res = @{ ok = $false; error = "Unhandled op" }
                try {
                    if ($op -eq "run_command") {
                        $res = Run-ForegroundCommand $pl
                    } elseif ($op -eq "start_command") {
                        $res = Start-BackgroundJob $pl
                    } elseif ($op -eq "read_command") {
                        $res = Read-BackgroundJob $pl
                    } elseif ($op -eq "stop_command") {
                        $res = Stop-BackgroundJob $pl
                    } elseif ($op -eq "file_read") {
                        $fp = Resolve-WorkspacePath ([string]$pl.path)
                        if (Test-BlockedSystemPath $fp) {
                            $res = @{ ok = $false; error = "Blocked system path" }
                        } elseif (-not [System.IO.File]::Exists($fp)) {
                            $res = @{ ok = $false; error = ("File not found: " + $fp) }
                        } else {
                            $raw = [System.IO.File]::ReadAllBytes($fp)
                            Write-Host ("[FILE-GET] " + $fp + " (" + $raw.Length + " B)") -ForegroundColor DarkGreen
                            $res = @{ ok = $true; bytes = $raw.Length; data_b64 = [System.Convert]::ToBase64String($raw) }
                        }
                    } elseif ($op -eq "file_write") {
                        $fp = Resolve-WorkspacePath ([string]$pl.path)
                        if (Test-BlockedSystemPath $fp) {
                            $res = @{ ok = $false; error = "Blocked system path" }
                        } else {
                            $dir = [System.IO.Path]::GetDirectoryName($fp)
                            if ($dir -and -not [System.IO.Directory]::Exists($dir)) {
                                [System.IO.Directory]::CreateDirectory($dir) | Out-Null
                            }
                            $raw = [System.Convert]::FromBase64String([string]$pl.data_b64)
                            [System.IO.File]::WriteAllBytes($fp, $raw)
                            Write-Host ("[FILE-PUT] " + $fp + " (" + $raw.Length + " B, zero-BOM)") -ForegroundColor DarkCyan
                            $res = @{ ok = $true; path = $fp; bytes = $raw.Length }
                        }
                    } elseif ($op -eq "file_delete") {
                        $fp = Resolve-WorkspacePath ([string]$pl.path)
                        if (Test-BlockedSystemPath $fp) {
                            $res = @{ ok = $false; error = "Blocked system path" }
                        } else {
                            if ([System.IO.File]::Exists($fp)) { [System.IO.File]::Delete($fp) }
                            $res = @{ ok = $true; deleted = $fp }
                        }
                    }
                } catch {
                    $res = @{ ok = $false; error = $_.Exception.Message }
                }
                try {
                    Invoke-HubJson "POST" "/limb/reply" @{ "X-Limb-Secret" = $LimbSecret } @{ task_id = $tid; result = $res } 30 | Out-Null
                } catch {}
            }
        }
    }
} finally {
    foreach ($j in $BgJobs.Values) {
        try {
            if (-not $j.Proc.HasExited) { & taskkill.exe /T /F /PID $j.Pid 2>$null | Out-Null }
            if (Test-Path -LiteralPath $j.LogFile) { Remove-Item -LiteralPath $j.LogFile -Force -ErrorAction SilentlyContinue }
        } catch {}
    }
    try {
        Invoke-HubJson "POST" "/limb/disconnect" @{ "X-Limb-Secret" = $LimbSecret } @{} 5 | Out-Null
    } catch {}
}
