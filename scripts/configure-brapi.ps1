$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$folder = Join-Path $root '.sites-runtime'
[void][IO.Directory]::CreateDirectory($folder)
$key = Read-Host 'Chave brapi (entrada protegida)' -AsSecureString
$credential = [PSCredential]::new('brapi', $key)
$credential | Export-Clixml -LiteralPath (Join-Path $folder 'brapi.credential.xml')
Write-Output 'Credencial protegida pelo Windows, disponivel somente ao usuario local.'
