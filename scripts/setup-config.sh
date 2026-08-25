#!/bin/bash
# Mini Agent Configuration Setup Script
# This script helps you set up Mini Agent configuration files

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration directory
CONFIG_DIR="$HOME/.mini-agent/config"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_CONFIG_DIR="$SCRIPT_DIR/../mini_agent/config"

echo -e "${CYAN}╔════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   Mini Agent Configuration Setup              ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════════════╝${NC}"
echo ""

# Step 1: Create config directory
echo -e "${BLUE}[1/2]${NC} Creating configuration directory..."
if [ -d "$CONFIG_DIR" ]; then
    # Auto backup existing config
    BACKUP_DIR="$HOME/.mini-agent/config.backup.$(date +%Y%m%d_%H%M%S)"
    echo -e "${YELLOW}   Configuration directory exists, backing up to:${NC}"
    echo -e "${YELLOW}   $BACKUP_DIR${NC}"
    cp -r "$CONFIG_DIR" "$BACKUP_DIR"
    echo -e "${GREEN}   ✓ Backup created${NC}"
else
    mkdir -p "$CONFIG_DIR"
    echo -e "${GREEN}   ✓ Created: $CONFIG_DIR${NC}"
fi

# Step 2: Copy the configuration shipped with this checkout
echo -e "${BLUE}[2/2]${NC} Copying configuration templates..."

if [ ! -f "$SOURCE_CONFIG_DIR/config-example.yaml" ]; then
    echo -e "${RED}   ✗ Cannot find templates in: $SOURCE_CONFIG_DIR${NC}"
    echo -e "${YELLOW}   Run this script from a Mini-Agent checkout.${NC}"
    exit 1
fi

cp "$SOURCE_CONFIG_DIR/config-example.yaml" "$CONFIG_DIR/config.yaml"
cp "$SOURCE_CONFIG_DIR/mcp-example.json" "$CONFIG_DIR/mcp.json"
cp "$SOURCE_CONFIG_DIR/system_prompt.md" "$CONFIG_DIR/system_prompt.md"
echo -e "${GREEN}   ✓ Configuration files ready${NC}"

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Setup Complete! ✨                          ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Configuration files location:"
echo -e "  ${CYAN}$CONFIG_DIR${NC}"
echo ""
echo -e "Files:"
ls -1 "$CONFIG_DIR" 2>/dev/null | sed 's/^/  📄 /' || echo "  (no files yet)"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo ""
echo -e "${YELLOW}1. Configure the model adapter:${NC}"
echo -e "   Edit config.yaml and set adapter, API key, exact endpoint, model, and output limit:"
echo -e "   ${GREEN}nano $CONFIG_DIR/config.yaml${NC}"
echo -e "   ${GREEN}vim $CONFIG_DIR/config.yaml${NC}"
echo -e "   ${GREEN}code $CONFIG_DIR/config.yaml${NC}"
echo ""
echo -e "${YELLOW}2. Start using Mini Agent:${NC}"
echo -e "   ${GREEN}mini-agent${NC}                              # Use current directory"
echo -e "   ${GREEN}mini-agent --workspace /path/to/project${NC} # Specify workspace"
echo -e "   ${GREEN}mini-agent --help${NC}                      # Show help"
echo ""
