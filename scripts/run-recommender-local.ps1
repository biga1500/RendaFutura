param([string]$NodePath, [int]$WebPort = 5175, [int]$EnginePort = 8765, [string]$DataDirectory, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (-not $NodePath) {
    $bundledNode = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
    $candidates = @($bundledNode) + @(Get-Command node -CommandType Application -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
    $NodePath = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not (Test-Path -LiteralPath $NodePath)) { throw 'Node nao encontrado. Informe -NodePath com o executavel Node.' }
$python = Join-Path $root '.recommender-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Crie o ambiente Python e instale recommender/requirements.txt.' }
if ($CheckOnly) { @{node=$NodePath; python=$python; nodeVersion=(& $NodePath --version)} | ConvertTo-Json; return }
function AvailablePort([int]$start) {
    for ($port = $start; $port -lt $start + 30; $port++) {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
        try { $listener.Start(); return $port } catch {} finally { $listener.Stop() }
    }
    throw 'Nenhuma porta local livre.'
}
$WebPort = AvailablePort $WebPort
$EnginePort = AvailablePort $EnginePort
$runtime = Join-Path $root '.sites-runtime'
[void][IO.Directory]::CreateDirectory($runtime)
$bytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$rng.Dispose()
$oldToken = $env:RECOMMENDER_TOKEN
$oldUrl = $env:RECOMMENDER_URL
$oldPath = $env:PATH
$oldData = $env:RF_DATA_DIR
$oldCurrentContracts = $env:RF_CURRENT_CONTRACT_PORTFOLIOS
$engine = $null
$web = $null
try {
    $env:RECOMMENDER_TOKEN = [Convert]::ToBase64String($bytes)
    $env:RECOMMENDER_URL = "http://127.0.0.1:$EnginePort"
    $env:RF_CURRENT_CONTRACT_PORTFOLIOS = '1'
    if (-not $DataDirectory) { $DataDirectory = Join-Path $root 'recommender\data\market' }
    if ((Test-Path -LiteralPath (Join-Path $DataDirectory 'current.json')) -or (Test-Path -LiteralPath (Join-Path $DataDirectory 'assets.json'))) {
        $env:RF_DATA_DIR = (Resolve-Path -LiteralPath $DataDirectory).Path
    }
    $env:PATH = (Split-Path $NodePath -Parent) + ';' + $env:PATH
    $engine = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','recommender.api:app','--host','127.0.0.1','--port',"$EnginePort",'--no-access-log') -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'motor.stdout.log') -RedirectStandardError (Join-Path $runtime 'motor.stderr.log')
    $webScript = Join-Path $root 'scripts\run-framework.mjs'
    $web = Start-Process -FilePath $NodePath -ArgumentList @(('"' + $webScript + '"'),'dev','--port',"$WebPort",'--hostname','127.0.0.1') -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'web.stdout.log') -RedirectStandardError (Join-Path $runtime 'web.stderr.log')
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        $engine.Refresh(); $web.Refresh()
        if ($engine.HasExited -or $web.HasExited) { throw 'Um servico encerrou. Confira os logs em .sites-runtime.' }
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$EnginePort/health" -TimeoutSec 2
            $page = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$WebPort/" -TimeoutSec 2
            if ($health.StatusCode -eq 200 -and $page.StatusCode -eq 200) { $ready = $true; break }
        } catch {}
    }
    if (-not $ready) { throw 'Os servicos nao ficaram prontos dentro do prazo.' }
    $state = @{url="http://127.0.0.1:$WebPort"; engineUrl="http://127.0.0.1:$EnginePort"; webPid=$web.Id; enginePid=$engine.Id; startedAt=(Get-Date).ToString('o'); root=$root; dataDirectory=$env:RF_DATA_DIR}
    $state | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime 'recommender-local.json') -Encoding utf8
    $state | ConvertTo-Json
} catch {
    if ($web -and -not $web.HasExited) { Stop-Process -Id $web.Id }
    if ($engine -and -not $engine.HasExited) { Stop-Process -Id $engine.Id }
    throw
} finally {
    $env:RECOMMENDER_TOKEN = $oldToken
    $env:RECOMMENDER_URL = $oldUrl
    $env:PATH = $oldPath
    $env:RF_DATA_DIR = $oldData
    $env:RF_CURRENT_CONTRACT_PORTFOLIOS = $oldCurrentContracts
}
