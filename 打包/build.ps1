param([switch]$CompanyPreset)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$workRoot = Join-Path $projectRoot '_测试\20261116_EXE打包'
$env:PYTHONIOENCODING = 'utf-8'
$env:TEMP = Join-Path $workRoot 'temp'
$env:TMP = $env:TEMP
$env:PYINSTALLER_CONFIG_DIR = Join-Path $workRoot 'pyinstaller-cache'
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
$savedPreset = $env:WINDOW_STUDIO_COMPANY_PRESET
try {
    $dist = Join-Path $projectRoot '发布\自动创建窗口_20261116'
    $work = Join-Path $workRoot 'build'
    if ($CompanyPreset) {
        $env:WINDOW_STUDIO_COMPANY_PRESET = Join-Path $projectRoot '_测试\20261116_公司预置版\私有快照\config.json'
        & $python (Join-Path $PSScriptRoot 'prepare_company_preset.py') $env:WINDOW_STUDIO_COMPANY_PRESET
        if ($LASTEXITCODE -ne 0) { throw 'Company configuration snapshot failed' }
        $dist = Join-Path $projectRoot '发布\自动创建窗口_公司内部版_20261116'
        $work = Join-Path $projectRoot '_测试\20261116_公司预置版\build'
    } else {
        $env:WINDOW_STUDIO_COMPANY_PRESET = ''
    }
    & $python -m PyInstaller --noconfirm --workpath $work --distpath $dist (Join-Path $PSScriptRoot 'window_studio.spec')
} finally {
    $env:WINDOW_STUDIO_COMPANY_PRESET = $savedPreset
}
if ($LASTEXITCODE -ne 0) { throw "EXE build failed: $LASTEXITCODE" }
