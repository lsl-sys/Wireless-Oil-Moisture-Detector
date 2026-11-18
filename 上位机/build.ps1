# Package the app into a standalone single-file exe.
# Thin wrapper: the real logic lives in build.py (Python handles UTF-8 names reliably,
#   whereas Windows PowerShell 5.1 would misread Chinese literals in this script).
# Usage: powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& "D:\anaconda3\python.exe" "$here\build.py"
