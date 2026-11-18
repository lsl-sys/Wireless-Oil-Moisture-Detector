# One-click flash entry.
# The real logic lives in flash.py (Python handles UTF-8 output reliably).
# Usage:
#   powershell -ExecutionPolicy Bypass -File flash.ps1
#   powershell -ExecutionPolicy Bypass -File flash.ps1 COM7
#   powershell -ExecutionPolicy Bypass -File flash.ps1 --erase
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& "D:\anaconda3\python.exe" "$here\flash.py" @args
