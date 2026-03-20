param(
  [string]$WorkerDir,
  [switch]$NoBuild,
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$WorkerArgs
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
$envFile = Join-Path $repoRoot ".env"

if (-not (Test-Path $envFile)) {
  throw "Missing $envFile. Copy .env.example to .env first."
}

$config = @{}
Get-Content $envFile | ForEach-Object {
  $line = $_.Trim()
  if (-not $line -or $line.StartsWith("#")) { return }
  $parts = $line -split "=", 2
  if ($parts.Count -ne 2) { return }
  $key = $parts[0].Trim()
  $value = $parts[1].Trim()
  if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
    $value = $value.Substring(1, $value.Length - 2)
  }
  $config[$key] = $value
}

if (-not $config.ContainsKey("WORKER_TOKEN") -or [string]::IsNullOrWhiteSpace($config["WORKER_TOKEN"])) {
  throw "WORKER_TOKEN is missing in .env"
}

if (-not $WorkerDir) {
  if ($env:SENTINELCAM_WORKER_DIR) {
    $WorkerDir = $env:SENTINELCAM_WORKER_DIR
  } else {
    $WorkerDir = Join-Path $repoRoot "..\sentinelCam-worker"
  }
}

$resolvedWorkerDir = Resolve-Path $WorkerDir -ErrorAction Stop
$runScript = Join-Path $resolvedWorkerDir "run.bat"
if (-not (Test-Path $runScript)) {
  throw "Expected worker launcher at $runScript"
}

$webPort = if ($config.ContainsKey("WEB_PORT") -and $config["WEB_PORT"]) { $config["WEB_PORT"] } else { "3000" }

Push-Location $repoRoot
try {
  if ($NoBuild) {
    docker compose up -d web
  } else {
    docker compose up -d --build web
  }
} finally {
  Pop-Location
}

$env:WEB_AUTH_TOKEN = $config["WORKER_TOKEN"]
$env:WEB_ALLOWED_ORIGINS = "http://localhost:$webPort,http://127.0.0.1:$webPort"

$argsList = @("--no-window", "--stream", "auto")
if ($config.ContainsKey("WORKER_BIND_HOST") -and $config["WORKER_BIND_HOST"]) {
  $argsList += @("--host", $config["WORKER_BIND_HOST"])
}
if ($config.ContainsKey("WORKER_SOURCE") -and $config["WORKER_SOURCE"]) {
  $argsList += @("--source", $config["WORKER_SOURCE"])
}
if ($WorkerArgs) {
  $argsList += $WorkerArgs
}

Write-Host "Web UI: http://localhost:$webPort"
Write-Host "Worker repo: $resolvedWorkerDir"
Write-Host "Allowed origins: $env:WEB_ALLOWED_ORIGINS"
Write-Host "Launching local worker..."

Push-Location $resolvedWorkerDir
try {
  & $runScript @argsList
} finally {
  Pop-Location
}
