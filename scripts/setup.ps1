# One-command setup for self-hosting nd-world on Windows — the PowerShell
# counterpart of scripts/setup.sh (which needs bash/openssl). Run from a
# normal PowerShell window; Docker Desktop must already be installed.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
# What this does: checks Docker, creates .env with a random session secret
# (never overwrites an existing one), asks for your GM login, builds and
# starts the stack, and waits for /health.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Join-Path $PSScriptRoot "..")

Write-Host "== N&D World — self-host setup (Windows) ==" -ForegroundColor Cyan
Write-Host

# --- 1. Check Docker -------------------------------------------------------
try { docker info *> $null } catch {
    Write-Host "Error: Docker isn't running. Start Docker Desktop first." -ForegroundColor Red
    exit 1
}
try { docker compose version *> $null } catch {
    Write-Host "Error: 'docker compose' isn't available (needs the Compose plugin)." -ForegroundColor Red
    exit 1
}
Write-Host "  Docker is available."

# --- 2. Create .env if needed ----------------------------------------------
if (Test-Path -LiteralPath ".env") {
    Write-Host "  .env already exists - leaving it as-is."
}
else {
    Write-Host "  Creating .env..."
    Copy-Item ".env.example" ".env"

    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $secretKey = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""

    $envLines = Get-Content ".env"
    $envLines = $envLines | ForEach-Object {
        if ($_ -match "^SECRET_KEY=") { "SECRET_KEY=$secretKey" }
        elseif ($_ -match "^GM_EMAIL=") { "GM_EMAIL=" + (Read-Host "  GM email") }
        elseif ($_ -match "^GM_PASSWORD=") {
            $pw = Read-Host "  GM password (min 8 characters)" -AsSecureString
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
            "GM_PASSWORD=$plain"
        }
        else { $_ }
    }
    Set-Content ".env" $envLines
    Write-Host "  Done. (AI chat/image gen stay off - set COMPOSE_PROFILES in .env later to enable.)"
}

# --- 3. Build and start ------------------------------------------------------
Write-Host
Write-Host "Building and starting the stack (this can take a few minutes the first time)..." -ForegroundColor Cyan
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Write-Host "Error: docker compose failed." -ForegroundColor Red; exit 1 }

# --- 4. Wait for it to come up ------------------------------------------------
$appPort = "8080"
foreach ($line in Get-Content ".env") {
    if ($line -match "^APP_PORT=(.+)$") { $appPort = $Matches[1].Trim() }
}
Write-Host
Write-Host "Waiting for nd-world to become healthy" -NoNewline
$ready = $false
foreach ($i in 1..60) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$appPort/health" -UseBasicParsing -TimeoutSec 3
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}
Write-Host
if (-not $ready) {
    Write-Host "It's taking longer than expected. Check: docker compose logs world" -ForegroundColor Yellow
    exit 1
}

Write-Host
Write-Host "== It's running! Open http://localhost:$appPort ==" -ForegroundColor Green
Write-Host "  Log in with the GM email/password you set (or edit .env and re-run"
Write-Host "  'docker compose up -d' to bootstrap the GM account)."
Write-Host "  To invite players: log in as GM, open a world's Edit page, Invite Links."
