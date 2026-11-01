param(
    [string]$PythonPath = "",
    [switch]$SkipInstall,
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$distDir = if ($OutputDir) { [System.IO.Path]::GetFullPath($OutputDir) } else { Join-Path $projectRoot "dist" }
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$python = if ($PythonPath) { $PythonPath } elseif (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }
$assetDir = Join-Path $projectRoot "assets"
$iconPath = Join-Path $assetDir "ZHS-Assistant.ico"
$versionPath = Join-Path $projectRoot "packaging\version_info.txt"

if (-not $SkipInstall) {
    Write-Host "[1/4] Installing runtime and build dependencies..."
    & $python -m pip install -r (Join-Path $projectRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
} else {
    Write-Host "[1/4] Using existing dependencies..."
}

# Some Python distributions load an additional PEM next to ssl.py. Preserve it
# in the frozen ssl module's directory, rather than changing certificate checks.
$sslPemPath = & $python -c "import ssl; from pathlib import Path; print(Path(ssl.__file__).with_name('codex-rejected-cas.pem'))"
if ($LASTEXITCODE -ne 0) { throw "Unable to inspect Python TLS resources." }
$tlsDataArgs = @()
if (Test-Path -LiteralPath $sslPemPath) {
    $tlsDataArgs = @("--add-data", "$sslPemPath;.")
}
$env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot "build\pyinstaller-cache"

Write-Host "[2/4] Generating application icon..."
New-Item -ItemType Directory -Path $assetDir -Force | Out-Null
& $python (Join-Path $projectRoot "tools\make_icon.py") $iconPath
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

Write-Host "[3/4] Building single-file executable..."
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "ZHS-Desktop" `
    --icon $iconPath `
    --version-file $versionPath `
    --add-data "$projectRoot\web;web" `
    --add-data "$projectRoot\meta.json;." `
    --add-data "$projectRoot\README.md;." `
    @tlsDataArgs `
    --collect-all "certifi" `
    --collect-all "openai" `
    --collect-all "tiktoken" `
    --collect-all "PIL" `
    --collect-all "webview" `
    --collect-all "pythonnet" `
    --collect-all "clr_loader" `
    --hidden-import "webview.platforms.edgechromium" `
    --hidden-import "homework_browser" `
    --collect-all "websocket" `
    --exclude-module "numpy" `
    --exclude-module "pandas" `
    --distpath $distDir `
    --workpath (Join-Path $projectRoot "build") `
    --specpath (Join-Path $projectRoot "build") `
    (Join-Path $projectRoot "zhs_app.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$exePath = Join-Path $distDir "ZHS-Desktop.exe"
Write-Host "[4/4] Build complete: $exePath"
Write-Host "Double-click the desktop executable. Runtime data will be stored beside it."
