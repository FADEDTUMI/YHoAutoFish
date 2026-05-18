param(
    [switch]$Plain,
    [switch]$SkipInstall,
    [string]$Notes,
    [string]$NotesFile,
    [string]$GiteeTag,
    [int]$GiteePartSizeMB = 100,
    [switch]$NoGiteeParts
)

$ErrorActionPreference = "Stop"

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
$VersionInfoPath = Join-Path $ProjectRoot "version_info.txt"
$VersionParts = @($AppVersion.Split(".") | ForEach-Object { [int]$_ })
while ($VersionParts.Count -lt 4) {
    $VersionParts += 0
}
$VersionTupleText = ($VersionParts[0..3] -join ", ")
$VersionFileText = "$($VersionParts[0]).$($VersionParts[1]).$($VersionParts[2]).$($VersionParts[3])"

Set-Location $ProjectRoot

if (Test-Path -LiteralPath $VersionInfoPath) {
    $VersionInfo = Get-Content -LiteralPath $VersionInfoPath -Raw -Encoding UTF8
    $VersionInfo = $VersionInfo -replace 'filevers=\([^)]+\)', "filevers=($VersionTupleText)"
    $VersionInfo = $VersionInfo -replace 'prodvers=\([^)]+\)', "prodvers=($VersionTupleText)"
    $VersionInfo = $VersionInfo -replace "StringStruct\('FileVersion', '[^']+'\)", "StringStruct('FileVersion', '$VersionFileText')"
    $VersionInfo = $VersionInfo -replace "StringStruct\('ProductVersion', '[^']+'\)", "StringStruct('ProductVersion', '$AppVersion')"
    $VersionInfo = $VersionInfo.TrimEnd() + [Environment]::NewLine
    Set-Content -LiteralPath $VersionInfoPath -Value $VersionInfo -Encoding UTF8 -NoNewline
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Get-MergedSha256 {
    param(
        [System.IO.FileInfo[]]$Files
    )

    $Sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Buffer = New-Object byte[] (1024 * 1024)
        foreach ($File in $Files) {
            $Stream = [System.IO.File]::OpenRead($File.FullName)
            try {
                while (($Read = $Stream.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
                    [void]$Sha.TransformBlock($Buffer, 0, $Read, $Buffer, 0)
                }
            } finally {
                $Stream.Dispose()
            }
        }
        [void]$Sha.TransformFinalBlock([byte[]]::new(0), 0, 0)
        return -join ($Sha.Hash | ForEach-Object { $_.ToString("x2") })
    } finally {
        $Sha.Dispose()
    }
}

function Split-ReleaseFile {
    param(
        [string]$SourcePath,
        [string]$OutputDirectory,
        [int64]$PartSizeBytes
    )

    if ($PartSizeBytes -le 0) {
        throw "PartSizeBytes must be greater than 0."
    }

    $SourceFile = Get-Item -LiteralPath $SourcePath
    $EscapedName = [Regex]::Escape($SourceFile.Name)
    Get-ChildItem -LiteralPath $OutputDirectory -File |
        Where-Object { $_.Name -match "^$EscapedName\.\d{3,4}$" } |
        Remove-Item -Force

    if ($SourceFile.Length -le $PartSizeBytes) {
        return @()
    }

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
                    if ($Read -le 0) {
                        break
                    }
                    $OutputStream.Write($Buffer, 0, $Read)
                    $Written += $Read
                }
            } finally {
                $OutputStream.Dispose()
            }
            $Result.Add((Get-Item -LiteralPath $PartPath))
            $PartIndex += 1
        }
    } finally {
        $InputStream.Dispose()
    }

    return @($Result.ToArray())
}

function Test-ForbiddenReleasePayload {
    param(
        [string]$PayloadRoot
    )

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
    param(
        [string]$ArchivePath
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $ForbiddenNames = @("auth_state.dat", "auth_device.json", "records.json")
    $ZipStream = [System.IO.File]::OpenRead($ArchivePath)
    try {
        $ZipArchive = New-Object System.IO.Compression.ZipArchive($ZipStream, [System.IO.Compression.ZipArchiveMode]::Read)
        try {
            foreach ($Entry in $ZipArchive.Entries) {
                $Name = [System.IO.Path]::GetFileName($Entry.FullName)
                if ($ForbiddenNames -contains $Name) {
                    throw "Forbidden release payload file: $($Entry.FullName)"
                }
            }
        } finally {
            $ZipArchive.Dispose()
        }
    } finally {
        $ZipStream.Dispose()
    }
}

if (-not $SkipInstall) {
    Invoke-Checked "python" @("-m", "pip", "install", "-r", "requirements.txt")
    Invoke-Checked "python" @("-m", "pip", "install", "-r", "requirements-build.txt")
}

Invoke-Checked "python" @("tools\make_icon.py")
Invoke-Checked "python" @("tools\prepare_ocr_models.py")

if ($Plain) {
    Invoke-Checked "python" @("-m", "PyInstaller", "--clean", "--noconfirm", ".\YHoAutoFish.spec")
} else {
    Invoke-Checked "pyarmor" @("gen", "-O", ".\build_obf", "-r", ".\main.py", ".\core")
    Invoke-Checked "python" @("-m", "PyInstaller", "--clean", "--noconfirm", ".\YHoAutoFish.obf.spec")
}

$CandidateDirs = @(
    (Join-Path $ProjectRoot "dist\$AppName"),
    (Join-Path $ProjectRoot ".pyarmor\pack\dist\$AppName")
)

$DistDir = $null
foreach ($Candidate in $CandidateDirs) {
    if (Test-Path (Join-Path $Candidate "$AppName.exe")) {
        $DistDir = $Candidate
        break
    }
}

if (-not $DistDir) {
    throw "Build finished, but $AppName.exe was not found in expected dist directories."
}

$UpdaterDistDir = Join-Path $ProjectRoot "dist\updater"
$UpdaterWorkDir = Join-Path $ProjectRoot "build\updater"
$UpdaterSpecDir = Join-Path $ProjectRoot "build\updater_spec"
Invoke-Checked "python" @(
    "-m", "PyInstaller",
    "--clean",
    "--noconfirm",
    "--onefile",
    "--noconsole",
    "--uac-admin",
    "--name", "YHoUpdater",
    "--icon", $IconPath,
    "--distpath", $UpdaterDistDir,
    "--workpath", $UpdaterWorkDir,
    "--specpath", $UpdaterSpecDir,
    ".\tools\updater.py"
)

$UpdaterExe = Join-Path $UpdaterDistDir "YHoUpdater.exe"
if (-not (Test-Path -LiteralPath $UpdaterExe)) {
    throw "Updater build finished, but YHoUpdater.exe was not found."
}
Copy-Item -LiteralPath $UpdaterExe -Destination (Join-Path $DistDir "YHoUpdater.exe") -Force

$BundledRecords = Join-Path $DistDir "records.json"
if (Test-Path -LiteralPath $BundledRecords) {
    Remove-Item -LiteralPath $BundledRecords -Force
}
Test-ForbiddenReleasePayload -PayloadRoot $DistDir

New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
Compress-Archive -Path $DistDir -DestinationPath $ZipPath -Force
Test-ForbiddenReleaseZip -ArchivePath $ZipPath

$ZipHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$SplitPartFiles = @()
if (-not $NoGiteeParts -and -not [string]::IsNullOrWhiteSpace($GiteeRepositoryUrl)) {
    $PartSizeBytes = [int64]$GiteePartSizeMB * 1024 * 1024
    $SplitPartFiles = @(Split-ReleaseFile -SourcePath $ZipPath -OutputDirectory $ReleaseDir -PartSizeBytes $PartSizeBytes)
}
$ManifestPath = Join-Path $ReleaseDir "latest.json"
$ReleaseNotes = ""
if (-not [string]::IsNullOrWhiteSpace($NotesFile)) {
    $ResolvedNotesFile = $NotesFile
    if (-not [System.IO.Path]::IsPathRooted($ResolvedNotesFile)) {
        $ResolvedNotesFile = Join-Path $ProjectRoot $ResolvedNotesFile
    }
    if (-not (Test-Path -LiteralPath $ResolvedNotesFile)) {
        throw "Notes file was not found: $NotesFile"
    }
    $ReleaseNotes = [string](Get-Content -LiteralPath $ResolvedNotesFile -Raw -Encoding UTF8)
} elseif (-not [string]::IsNullOrWhiteSpace($Notes)) {
    $ReleaseNotes = $Notes
}
$GitHubTag = "v$AppVersion"
if ([string]::IsNullOrWhiteSpace($GiteeTag)) {
    $GiteeTag = $AppVersion
}
$GiteeTag = $GiteeTag.Trim()
$GitHubReleaseUrl = "$RepositoryUrl/releases/tag/$GitHubTag"
$GiteeReleaseUrl = if (-not [string]::IsNullOrWhiteSpace($GiteeRepositoryUrl) -and -not [string]::IsNullOrWhiteSpace($GiteeTag)) {
    "$GiteeRepositoryUrl/releases/tag/$GiteeTag"
} else {
    ""
}
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
                    download_urls = @(
                        "$RepositoryUrl/releases/download/$GitHubTag/$($PartFile.Name)"
                    )
                    gitee_download_urls = @(
                        "$GiteeRepositoryUrl/releases/download/$GiteeTag/$($PartFile.Name)"
                    )
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
[System.IO.File]::WriteAllText($ManifestPath, $ManifestJson, $Utf8NoBom)

Write-Host "EXE: $(Join-Path $DistDir "$AppName.exe")"
Write-Host "ZIP: $ZipPath"
if ($SplitPartFiles.Count -gt 0) {
    Write-Host "GITEE PARTS:"
    foreach ($PartFile in $SplitPartFiles) {
        Write-Host "  $($PartFile.FullName)"
    }
}
Write-Host "MANIFEST: $ManifestPath"
