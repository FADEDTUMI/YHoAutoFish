param(
    [switch]$Onefile,
    [switch]$SkipInstall,
    [string]$Notes,
    [string]$NotesFile,
    [string]$GiteeTag,
    [int]$GiteePartSizeMB = 100,
    [switch]$NoGiteeParts
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Paths & version
# ---------------------------------------------------------------------------
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppName = "YHoAutoFish"
$VersionSource = Get-Content -LiteralPath (Join-Path $ProjectRoot "core\version.py") -Raw -Encoding UTF8
if ($VersionSource -notmatch 'APP_VERSION\s*=\s*"([^"]+)"') {
    throw "Unable to read APP_VERSION from core\version.py"
}
$AppVersion = $Matches[1]
if ($VersionSource -notmatch 'APP_REPOSITORY_URL\s*=\s*"([^"]+)"') {
    throw "Unable to read APP_REPOSITORY_URL from core\version.py"
}
$RepositoryUrl = $Matches[1].TrimEnd("/")
$GiteeRepositoryUrl = ""
if ($VersionSource -match 'APP_GITEE_REPOSITORY_URL\s*=\s*"([^"]+)"') {
    $GiteeRepositoryUrl = $Matches[1].TrimEnd("/")
}
$ReleaseDir = Join-Path $ProjectRoot "release"
$ZipName = "$AppName-v$AppVersion-windows.zip"
$ZipPath = Join-Path $ReleaseDir $ZipName
$IconPath = Join-Path $ProjectRoot "build_assets\logo.ico"

Set-Location $ProjectRoot

# Use py launcher to ensure Python 3.12 (not 3.14)
$Python = "py"
$PyArgs = @("-3.12")

# Redirect compiler caches to D: to avoid C: disk space issues
$NuitkCacheDir = Join-Path $ProjectRoot "build\nuitka_cache"
$env:NUITKA_CACHE_DIR = $NuitkCacheDir
$env:CLCACHE_DIR = Join-Path $NuitkCacheDir "clcache"
New-Item -ItemType Directory -Force -Path $NuitkCacheDir | Out-Null
New-Item -ItemType Directory -Force -Path $env:CLCACHE_DIR | Out-Null

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    Write-Host ">> $FilePath $($Arguments -join ' ')" -ForegroundColor DarkGray
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Py {
    param([string[]]$Arguments)
    Invoke-Checked $Python ($PyArgs + $Arguments)
}

# ---------------------------------------------------------------------------
# SHA256 / split helpers
# ---------------------------------------------------------------------------
function Get-MergedSha256 {
    param([System.IO.FileInfo[]]$Files)
    $Sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Buffer = New-Object byte[] (1024 * 1024)
        foreach ($File in $Files) {
            $Stream = [System.IO.File]::OpenRead($File.FullName)
            try {
                while (($Read = $Stream.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
                    [void]$Sha.TransformBlock($Buffer, 0, $Read, $Buffer, 0)
                }
            } finally { $Stream.Dispose() }
        }
        [void]$Sha.TransformFinalBlock([byte[]]::new(0), 0, 0)
        return -join ($Sha.Hash | ForEach-Object { $_.ToString("x2") })
    } finally { $Sha.Dispose() }
}

function Split-ReleaseFile {
    param(
        [string]$SourcePath,
        [string]$OutputDirectory,
        [int64]$PartSizeBytes
    )
    if ($PartSizeBytes -le 0) { throw "PartSizeBytes must be > 0." }
    $SourceFile = Get-Item -LiteralPath $SourcePath
    $EscapedName = [Regex]::Escape($SourceFile.Name)
    Get-ChildItem -LiteralPath $OutputDirectory -File |
        Where-Object { $_.Name -match "^$EscapedName\.\d{3,4}$" } |
        Remove-Item -Force
    if ($SourceFile.Length -le $PartSizeBytes) { return @() }
    $Result = New-Object System.Collections.Generic.List[System.IO.FileInfo]
    $Buffer = New-Object byte[] (1024 * 1024)
    $InputStream = [System.IO.File]::OpenRead($SourceFile.FullName)
    try {
        $PartIndex = 1
        while ($InputStream.Position -lt $InputStream.Length) {
            $PartName = "{0}.{1:000}" -f $SourceFile.Name, $PartIndex
            $PartPath = Join-Path $OutputDirectory $PartName
            $OutputStream = [System.IO.File]::Create($PartPath)
            try {
                $Written = [int64]0
                while ($Written -lt $PartSizeBytes -and $InputStream.Position -lt $InputStream.Length) {
                    $Remaining = [Math]::Min([int64]$Buffer.Length, $PartSizeBytes - $Written)
                    $Read = $InputStream.Read($Buffer, 0, [int]$Remaining)
                    if ($Read -le 0) { break }
                    $OutputStream.Write($Buffer, 0, $Read)
                    $Written += $Read
                }
            } finally { $OutputStream.Dispose() }
            $Result.Add((Get-Item -LiteralPath $PartPath))
            $PartIndex++
        }
    } finally { $InputStream.Dispose() }
    return @($Result.ToArray())
}

function Test-ForbiddenReleasePayload {
    param([string]$PayloadRoot)
    $ForbiddenNames = @("auth_state.dat", "auth_device.json", "records.json")
    foreach ($Name in $ForbiddenNames) {
        $Matches = @(Get-ChildItem -LiteralPath $PayloadRoot -Recurse -File -Filter $Name -ErrorAction SilentlyContinue)
        if ($Matches.Count -gt 0) {
            $Paths = ($Matches | ForEach-Object { $_.FullName }) -join "; "
            throw "Forbidden release payload file: $Name ($Paths)"
        }
    }
}

function Test-ForbiddenReleaseZip {
    param([string]$ArchivePath)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $ForbiddenNames = @("auth_state.dat", "auth_device.json", "records.json")
    $ZipStream = [System.IO.File]::OpenRead($ArchivePath)
    try {
        $ZipArchive = New-Object System.IO.Compression.ZipArchive($ZipStream, [System.IO.Compression.ZipArchiveMode]::Read)
        try {
            foreach ($Entry in $ZipArchive.Entries) {
                $Name = [System.IO.Path]::GetFileName($Entry.FullName)
                if ($ForbiddenNames -contains $Name) {
                    throw "Forbidden release payload file in zip: $($Entry.FullName)"
                }
            }
        } finally { $ZipArchive.Dispose() }
    } finally { $ZipStream.Dispose() }
}

# ---------------------------------------------------------------------------
# Step 1: Install dependencies
# ---------------------------------------------------------------------------
if (-not $SkipInstall) {
    Invoke-Py @("-m", "pip", "install", "-r", "requirements.txt")
    Invoke-Py @("-m", "pip", "install", "-r", "requirements-build.txt")
}

# ---------------------------------------------------------------------------
# Step 2: Prepare build assets
# ---------------------------------------------------------------------------
Invoke-Py @("tools\make_icon.py")
Invoke-Py @("tools\prepare_ocr_models.py")

# ---------------------------------------------------------------------------
# Step 3: Nuitka build
# ---------------------------------------------------------------------------
$NuitkMode = if ($Onefile) { "--onefile" } else { "--standalone" }

$NuitkArgs = @(
    "-m", "nuitka",
    $NuitkMode,

    # Plugins
    "--enable-plugin=pyside6",
    "--include-qt-plugins=sensible",

    # Windows metadata
    "--windows-console-mode=disable",
    "--windows-uac-admin",
    "--windows-icon-from-ico=$IconPath",

    # Output
    "--output-dir=build\nuitka",
    "--output-filename=$AppName.exe",

    # Auto-download MinGW64 / dependencies
    "--assume-yes-for-downloads",

    # Packages to include (C extensions + runtime conditionals)
    "--include-package=PySide6",
    "--include-package=cv2",
    "--include-package=numpy",
    "--include-package=PIL",
    "--include-package=mss",
    "--include-package=onnxruntime",
    "--include-package=cnocr",
    "--include-package=cnstd",
    "--include-package=rapidocr",
    "--include-package=websocket",
    "--include-module=win32crypt",
    "--include-module=win32gui",
    "--include-module=win32process",
    "--include-module=win32api",
    "--include-module=pydirectinput",
    "--include-module=lxml",

    # Data files / resources
    "--include-data-dir=assets=assets",
    "--include-data-dir=ocr_models=ocr_models",
    "--include-data-dir=sponsor_qr=sponsor_qr",
    "--include-data-dir=certs=certs",
    "--include-data-files=logo.jpg=logo.jpg",
    "--include-data-files=build_assets/logo.ico=logo.ico",
    "--include-data-files=config.json=config.json"
)

# Chinese-named directory (fish encyclopedia) - add if exists
$FishDirName = "异环鱼类图鉴资源"
$FishDirPath = Join-Path $ProjectRoot $FishDirName
if (Test-Path -LiteralPath $FishDirPath) {
    $NuitkArgs += "--include-data-dir=${FishDirName}=${FishDirName}"
}

# Onefile: keep data files external so users can edit config.json
if ($Onefile) {
    $NuitkArgs += @(
        "--include-onefile-external-data=assets",
        "--include-onefile-external-data=ocr_models",
        "--include-onefile-external-data=sponsor_qr",
        "--include-onefile-external-data=certs",
        "--include-onefile-external-data=logo.jpg",
        "--include-onefile-external-data=logo.ico",
        "--include-onefile-external-data=config.json"
    )
    if (Test-Path -LiteralPath $FishDirPath) {
        $NuitkArgs += "--include-onefile-external-data=${FishDirName}"
    }
}

# Anti-bloat: exclude heavy transitive dependencies from cnstd/cnocr
# cnstd pulls in torch (~2GB), torchvision, scipy, pandas, pytorch-lightning, ultralytics
# but our code only uses their onnxruntime inference path, not PyTorch
$NuitkArgs += @(
    # === cnstd/cnocr transitive bloat (torch ecosystem) ===
    "--nofollow-import-to=torch",
    "--nofollow-import-to=torchvision",
    "--nofollow-import-to=torchaudio",
    "--nofollow-import-to=pytorch_lightning",
    "--nofollow-import-to=lightning",
    "--nofollow-import-to=lightning_fabric",
    "--nofollow-import-to=ultralytics",
    "--nofollow-import-to=ultralytics_thop",
    # === Scientific computing bloat ===
    "--nofollow-import-to=scipy",
    "--nofollow-import-to=pandas",
    "--nofollow-import-to=sympy",
    "--nofollow-import-to=matplotlib",
    "--nofollow-import-to=IPython",
    "--nofollow-import-to=sklearn",
    "--nofollow-import-to=skimage",
    # === Qt modules not used ===
    "--nofollow-import-to=PySide6.QtWebEngine",
    "--nofollow-import-to=PySide6.QtWebEngineCore",
    "--nofollow-import-to=PySide6.QtWebEngineWidgets",
    "--nofollow-import-to=PySide6.Qt3D",
    "--nofollow-import-to=PySide6.QtQuick",
    "--nofollow-import-to=PySide6.QtQml",
    "--nofollow-import-to=PySide6.QtDesigner",
    "--nofollow-import-to=PySide6.QtHelp",
    "--nofollow-import-to=PySide6.QtMultimedia",
    "--nofollow-import-to=PySide6.QtSql",
    "--nofollow-import-to=PySide6.QtSvg",
    "--nofollow-import-to=PySide6.QtTest",
    "--nofollow-import-to=PySide6.QtUiTools",
    "--nofollow-import-to=PySide6.QtXml",
    # === Test/utility frameworks ===
    "--nofollow-import-to=numpy.tests",
    "--nofollow-import-to=numpy.f2py",
    "--nofollow-import-to=cv2.tests",
    "--noinclude-pytest-mode=nofollow",
    "--noinclude-setuptools-mode=nofollow",
    "--noinclude-unittest-mode=nofollow",
    # === Compiler settings ===
    "--low-memory",
    "--jobs=4"
)

# Entry point
$NuitkArgs += "main.py"

$BuildModeLabel = if ($Onefile) { "onefile" } else { "standalone" }
Write-Host "`n=== Nuitka build ($BuildModeLabel) ===" -ForegroundColor Cyan
Invoke-Py $NuitkArgs

# ---------------------------------------------------------------------------
# Step 4: Locate output
# ---------------------------------------------------------------------------
if (-not $Onefile) {
    $NuitkDistDir = Join-Path $ProjectRoot "build\nuitka\main.dist"
    if (-not (Test-Path (Join-Path $NuitkDistDir "$AppName.exe"))) {
        throw "Nuitka standalone build finished, but $AppName.exe not found in $NuitkDistDir"
    }
} else {
    $OnefileExe = Join-Path $ProjectRoot "build\nuitka\$AppName.exe"
    if (-not (Test-Path -LiteralPath $OnefileExe)) {
        throw "Nuitka onefile build finished, but $AppName.exe not found"
    }
    $NuitkDistDir = Join-Path $ProjectRoot "dist\nuitka"
    New-Item -ItemType Directory -Force -Path $NuitkDistDir | Out-Null
    Copy-Item -LiteralPath $OnefileExe -Destination (Join-Path $NuitkDistDir "$AppName.exe") -Force
    # Copy external data dirs that Nuitka placed alongside the exe
    foreach ($extDir in @("assets", "ocr_models", "sponsor_qr", "certs")) {
        $src = Join-Path $ProjectRoot "build\nuitka\$extDir"
        if (Test-Path -LiteralPath $src) {
            Copy-Item -LiteralPath $src -Destination (Join-Path $NuitkDistDir $extDir) -Recurse -Force
        }
    }
    foreach ($extFile in @("logo.jpg", "logo.ico", "config.json")) {
        $src = Join-Path $ProjectRoot "build\nuitka\$extFile"
        if (Test-Path -LiteralPath $src) {
            Copy-Item -LiteralPath $src -Destination (Join-Path $NuitkDistDir $extFile) -Force
        }
    }
}

Write-Host "`nDist dir: $NuitkDistDir" -ForegroundColor Green

# ---------------------------------------------------------------------------
# Step 5: Build YHoUpdater.exe (PyInstaller, onefile)
# ---------------------------------------------------------------------------
$UpdaterDistDir = Join-Path $ProjectRoot "dist\updater"
$UpdaterWorkDir = Join-Path $ProjectRoot "build\updater"
$UpdaterSpecDir = Join-Path $ProjectRoot "build\updater_spec"
Invoke-Py @(
    "-m", "PyInstaller",
    "--clean", "--noconfirm",
    "--onefile", "--noconsole", "--uac-admin",
    "--name", "YHoUpdater",
    "--icon", $IconPath,
    "--distpath", $UpdaterDistDir,
    "--workpath", $UpdaterWorkDir,
    "--specpath", $UpdaterSpecDir,
    ".\tools\updater.py"
)

$UpdaterExe = Join-Path $UpdaterDistDir "YHoUpdater.exe"
if (-not (Test-Path -LiteralPath $UpdaterExe)) {
    throw "Updater build finished, but YHoUpdater.exe not found."
}
Copy-Item -LiteralPath $UpdaterExe -Destination (Join-Path $NuitkDistDir "YHoUpdater.exe") -Force

# ---------------------------------------------------------------------------
# Step 6: Safety checks
# ---------------------------------------------------------------------------
$BundledRecords = Join-Path $NuitkDistDir "records.json"
if (Test-Path -LiteralPath $BundledRecords) {
    Remove-Item -LiteralPath $BundledRecords -Force
}
Test-ForbiddenReleasePayload -PayloadRoot $NuitkDistDir

# ---------------------------------------------------------------------------
# Step 7: Zip + manifest
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

# Stage files under YHoAutoFish/ parent directory for zip consistency
$StageDir = Join-Path $ProjectRoot "build\nuitka_stage"
if (Test-Path -LiteralPath $StageDir) { Remove-Item -LiteralPath $StageDir -Recurse -Force }
$StageAppDir = Join-Path $StageDir $AppName
New-Item -ItemType Directory -Force -Path $StageAppDir | Out-Null
Copy-Item -Path "$NuitkDistDir\*" -Destination $StageAppDir -Recurse -Force
Compress-Archive -Path "$StageDir\*" -DestinationPath $ZipPath -Force
Remove-Item -LiteralPath $StageDir -Recurse -Force
Test-ForbiddenReleaseZip -ArchivePath $ZipPath

$ZipHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$SplitPartFiles = @()
if (-not $NoGiteeParts -and -not [string]::IsNullOrWhiteSpace($GiteeRepositoryUrl)) {
    $PartSizeBytes = [int64]$GiteePartSizeMB * 1024 * 1024
    $SplitPartFiles = @(Split-ReleaseFile -SourcePath $ZipPath -OutputDirectory $ReleaseDir -PartSizeBytes $PartSizeBytes)
}

# Release notes
$ReleaseNotes = ""
if (-not [string]::IsNullOrWhiteSpace($NotesFile)) {
    $ResolvedNotesFile = $NotesFile
    if (-not [System.IO.Path]::IsPathRooted($ResolvedNotesFile)) {
        $ResolvedNotesFile = Join-Path $ProjectRoot $NotesFile
    }
    if (-not (Test-Path -LiteralPath $ResolvedNotesFile)) {
        throw "Notes file not found: $NotesFile"
    }
    $ReleaseNotes = [string](Get-Content -LiteralPath $ResolvedNotesFile -Raw -Encoding UTF8)
} elseif (-not [string]::IsNullOrWhiteSpace($Notes)) {
    $ReleaseNotes = $Notes
}

# Build manifest
$GitHubTag = "v$AppVersion"
if ([string]::IsNullOrWhiteSpace($GiteeTag)) { $GiteeTag = $AppVersion }
$GiteeTag = $GiteeTag.Trim()
$GitHubReleaseUrl = "$RepositoryUrl/releases/tag/$GitHubTag"
$GiteeReleaseUrl = if (-not [string]::IsNullOrWhiteSpace($GiteeRepositoryUrl) -and -not [string]::IsNullOrWhiteSpace($GiteeTag)) {
    "$GiteeRepositoryUrl/releases/tag/$GiteeTag"
} else { "" }

$SplitPartFiles = @($SplitPartFiles | Sort-Object Name)
$Manifest = [ordered]@{
    version = $AppVersion
    tag = $GitHubTag
    tag_name = $GitHubTag
    asset_name = $ZipName
    download_url = "$RepositoryUrl/releases/latest/download/$ZipName"
    download_urls = @(
        "$RepositoryUrl/releases/latest/download/$ZipName",
        "$RepositoryUrl/releases/download/$GitHubTag/$ZipName"
    )
    github_download_urls = @(
        "$RepositoryUrl/releases/latest/download/$ZipName",
        "$RepositoryUrl/releases/download/$GitHubTag/$ZipName"
    )
    html_url = $GitHubReleaseUrl
    github_html_url = $GitHubReleaseUrl
    sha256 = $ZipHash
    github_sha256 = $ZipHash
    notes = $ReleaseNotes
    mandatory = $false
    published_at = (Get-Date).ToString("o")
}
if (-not [string]::IsNullOrWhiteSpace($GiteeRepositoryUrl) -and -not [string]::IsNullOrWhiteSpace($GiteeTag)) {
    $Manifest["gitee_release_tag"] = $GiteeTag
    $Manifest["gitee_html_url"] = $GiteeReleaseUrl
    if ($SplitPartFiles.Count -gt 0) {
        $Manifest["gitee_download_urls"] = @()
        $Manifest["gitee_sha256"] = Get-MergedSha256 -Files $SplitPartFiles
        $Manifest["gitee_release_asset_names"] = @("latest.json") + @($SplitPartFiles | ForEach-Object { $_.Name })
        $Manifest["gitee_asset_parts"] = @(
            foreach ($PartFile in $SplitPartFiles) {
                [ordered]@{
                    name = $PartFile.Name
                    size = [int64]$PartFile.Length
                    sha256 = (Get-FileHash -LiteralPath $PartFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                    download_urls = @("$RepositoryUrl/releases/download/$GitHubTag/$($PartFile.Name)")
                    gitee_download_urls = @("$GiteeRepositoryUrl/releases/download/$GiteeTag/$($PartFile.Name)")
                }
            }
        )
    } else {
        $Manifest["gitee_download_urls"] = @(
            "$GiteeRepositoryUrl/releases/download/$GiteeTag/$ZipName"
        )
    }
}
$ManifestJson = ($Manifest | ConvertTo-Json -Depth 4) + [Environment]::NewLine
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$LatestJsonPath = Join-Path $ReleaseDir "latest.json"
[System.IO.File]::WriteAllText($LatestJsonPath, $ManifestJson, $Utf8NoBom)

Write-Host "`n=== Build complete ===" -ForegroundColor Green
Write-Host "EXE: $(Join-Path $NuitkDistDir "$AppName.exe")"
Write-Host "ZIP: $ZipPath"
if ($SplitPartFiles.Count -gt 0) {
    Write-Host "GITEE PARTS:"
    foreach ($PartFile in $SplitPartFiles) {
        Write-Host "  $($PartFile.FullName)"
    }
}
Write-Host "MANIFEST: $LatestJsonPath"
