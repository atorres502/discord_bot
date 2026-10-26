# Packs the bot, copies it to the CasaOS server, then rebuilds and restarts the container.
# Usage: .\deploy.ps1                 (uses DEPLOY_HOST from .env, e.g. DEPLOY_HOST=user@192.168.1.50)
#        .\deploy.ps1 user@192.168.1.50
#        .\deploy.ps1 -Fresh          (rebuild from scratch to pick up a newer yt-dlp)
param([string]$Target, [switch]$Fresh)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not $Target) {
    $line = Get-Content .env | Where-Object { $_ -match '^\s*DEPLOY_HOST\s*=' } | Select-Object -First 1
    if ($line) { $Target = ($line -split '=', 2)[1].Trim() }
}
if (-not $Target) {
    throw "No server given. Run .\deploy.ps1 user@host, or add DEPLOY_HOST=user@host to .env."
}

$archive = 'discbot.tar'
# .env rides along so the server gets the token; .dockerignore keeps it out of the image
$files = 'Dockerfile', 'docker-compose.yml', 'requirements.txt', 'discbot.py', '.env'

Write-Host "Packing files..."
tar -cf $archive @files
if ($LASTEXITCODE) { throw "tar failed" }

try {
    Write-Host "Copying to $Target..."
    scp $archive "${Target}:~/discbot.tar"
    if ($LASTEXITCODE) { throw "Copying to the server failed" }

    Write-Host "Rebuilding and restarting on the server (sudo may ask for your password)..."
    # Docker caches the pip install step, so packages only update on a -Fresh build
    $build = if ($Fresh) { 'sudo docker compose build --no-cache --pull && ' } else { '' }
    $remote = 'mkdir -p ~/discbot && tar -xf ~/discbot.tar -C ~/discbot && rm ~/discbot.tar' +
        ' && chmod 600 ~/discbot/.env && cd ~/discbot && ' + $build + 'sudo docker compose up -d --build' +
        ' && sleep 5 && echo "--- latest logs:" && sudo docker logs --tail 15 discbot'
    ssh -t $Target $remote
    if ($LASTEXITCODE) { throw "Deploy failed on the server" }
} finally {
    Remove-Item $archive -ErrorAction SilentlyContinue
}

Write-Host "Deployed."
