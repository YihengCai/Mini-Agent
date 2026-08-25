# Mini Agent Configuration Setup Script for Windows
# This script helps you set up Mini Agent configuration files

# Error handling
$ErrorActionPreference = "Stop"

# Colors for output
function Write-ColorOutput {
    param(
        [string]$Message,
        [string]$Color = "White"
    )

    $colorMap = @{
        "Red" = [ConsoleColor]::Red
        "Green" = [ConsoleColor]::Green
        "Yellow" = [ConsoleColor]::Yellow
        "Blue" = [ConsoleColor]::Blue
        "Cyan" = [ConsoleColor]::Cyan
        "White" = [ConsoleColor]::White
    }

    Write-Host $Message -ForegroundColor $colorMap[$Color]
}

# Configuration directory
$CONFIG_DIR = Join-Path $env:USERPROFILE ".mini-agent\config"
$SOURCE_CONFIG_DIR = Join-Path $PSScriptRoot "..\mini_agent\config"

Write-ColorOutput "==================================================" -Color "Cyan"
Write-ColorOutput "   Mini Agent Configuration Setup" -Color "Cyan"
Write-ColorOutput "==================================================" -Color "Cyan"
Write-Host ""

# Step 1: Create config directory
Write-ColorOutput "[1/2] Creating configuration directory..." -Color "Blue"

if (Test-Path $CONFIG_DIR) {
    # Auto backup existing config
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $BACKUP_DIR = Join-Path $env:USERPROFILE ".mini-agent\config.backup.$timestamp"
    Write-ColorOutput "   Configuration directory exists, backing up to:" -Color "Yellow"
    Write-ColorOutput "   $BACKUP_DIR" -Color "Yellow"
    Copy-Item -Path $CONFIG_DIR -Destination $BACKUP_DIR -Recurse
    Write-ColorOutput "   [OK] Backup created" -Color "Green"
} else {
    New-Item -Path $CONFIG_DIR -ItemType Directory -Force | Out-Null
    Write-ColorOutput "   [OK] Created: $CONFIG_DIR" -Color "Green"
}

# Step 2: Copy the configuration shipped with this checkout
Write-ColorOutput "[2/2] Copying configuration templates..." -Color "Blue"

$configTemplate = Join-Path $SOURCE_CONFIG_DIR "config-example.yaml"
if (-not (Test-Path $configTemplate)) {
    Write-ColorOutput "   [ERROR] Cannot find templates in: $SOURCE_CONFIG_DIR" -Color "Red"
    Write-ColorOutput "   Run this script from a Mini-Agent checkout." -Color "Yellow"
    exit 1
}

Copy-Item -Path $configTemplate -Destination (Join-Path $CONFIG_DIR "config.yaml") -Force
Copy-Item -Path (Join-Path $SOURCE_CONFIG_DIR "mcp-example.json") -Destination (Join-Path $CONFIG_DIR "mcp.json") -Force
Copy-Item -Path (Join-Path $SOURCE_CONFIG_DIR "system_prompt.md") -Destination (Join-Path $CONFIG_DIR "system_prompt.md") -Force
Write-ColorOutput "   [OK] Configuration files ready" -Color "Green"

Write-Host ""
Write-ColorOutput "==================================================" -Color "Green"
Write-ColorOutput "   Setup Complete!" -Color "Green"
Write-ColorOutput "==================================================" -Color "Green"
Write-Host ""
Write-Host "Configuration files location:"
Write-ColorOutput "  $CONFIG_DIR" -Color "Cyan"
Write-Host ""
Write-Host "Files:"
Get-ChildItem $CONFIG_DIR -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "   $($_.Name)"
}
Write-Host ""
Write-ColorOutput "Next Steps:" -Color "Yellow"
Write-Host ""
Write-ColorOutput "1. Configure the model adapter:" -Color "Yellow"
Write-Host "   Edit config.yaml and set adapter, API key, exact endpoint, model, and output limit:"
Write-ColorOutput "   notepad $CONFIG_DIR\config.yaml" -Color "Green"
Write-ColorOutput "   code $CONFIG_DIR\config.yaml" -Color "Green"
Write-Host ""
Write-ColorOutput "2. Start using Mini Agent:" -Color "Yellow"
Write-ColorOutput "   mini-agent                              # Use current directory" -Color "Green"
Write-ColorOutput "   mini-agent --workspace C:\path\to\project # Specify workspace" -Color "Green"
Write-ColorOutput "   mini-agent --help                      # Show help" -Color "Green"
Write-Host ""
