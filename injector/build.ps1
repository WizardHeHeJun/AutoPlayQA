# Builds gameinjector.dex from GameInjector.java using an Android toolchain
# (javac + d8 + android.jar). The layout below matches a Unity-bundled Android
# SDK/OpenJDK, so no standalone Android SDK / JDK is required — point
# $env:ANDROID_BUILD_TOOLCHAIN at whichever toolchain root you have (a plain
# Android SDK works too as long as it also ships an OpenJDK\bin\javac.exe).
#
# Usage:  powershell -ExecutionPolicy Bypass -File injector\build.ps1
# Output: injector\gameinjector.dex  (commit it so end users need no toolchain)

$ErrorActionPreference = "Stop"

# e.g. <unity-hub>\Editor\<version>\Editor\Data\PlaybackEngines\AndroidPlayer
$Toolchain = $env:ANDROID_BUILD_TOOLCHAIN
if (-not $Toolchain) {
    throw "Set ANDROID_BUILD_TOOLCHAIN to your Android toolchain root (see the header of this script)"
}
$Javac = Join-Path $Toolchain "OpenJDK\bin\javac.exe"
$Java  = Join-Path $Toolchain "OpenJDK\bin\java.exe"
$D8Jar = Join-Path $Toolchain "SDK\build-tools\34.0.0\lib\d8.jar"
$AndroidJar = Join-Path $Toolchain "SDK\platforms\android-34\android.jar"

foreach ($p in @($Javac, $Java, $D8Jar, $AndroidJar)) {
    if (-not (Test-Path $p)) { throw "Toolchain component not found: $p" }
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Classes = Join-Path $Root "build_classes"
$DexDir  = Join-Path $Root "build_dex"
$Src     = Join-Path $Root "GameInjector.java"

Remove-Item -Recurse -Force $Classes, $DexDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Classes, $DexDir | Out-Null

Write-Host "[1/3] javac ..."
& $Javac -encoding UTF-8 -classpath $AndroidJar -d $Classes $Src
if ($LASTEXITCODE -ne 0) { throw "javac failed" }

Write-Host "[2/3] d8 ..."
# Pass class files as a flat array ($ClassFiles, not @ClassFiles): PowerShell
# expands an array into separate native args, while splat (@) on a single-element
# scalar would be split character-by-character.
$ClassFiles = @(Get-ChildItem -Recurse -Filter *.class $Classes | ForEach-Object { $_.FullName })
& $Java -cp $D8Jar com.android.tools.r8.D8 --min-api 34 --lib $AndroidJar --output $DexDir $ClassFiles
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

Write-Host "[3/3] packaging ..."
$OutDex = Join-Path $Root "gameinjector.dex"
Copy-Item (Join-Path $DexDir "classes.dex") $OutDex -Force
Remove-Item -Recurse -Force $Classes, $DexDir -ErrorAction SilentlyContinue

Write-Host "Done -> $OutDex"
