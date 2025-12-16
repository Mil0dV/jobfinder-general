#!/bin/bash
# Quick setup script for Job Vacancy Finder Agent (using uv)

echo "=========================================="
echo "Job Vacancy Finder Agent - Quick Setup"
echo "=========================================="
echo ""

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "⚠️  uv is not installed. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "✓ uv installed"
    echo ""
    echo "Please restart your shell or run: source $HOME/.cargo/env"
    echo "Then run this script again."
    exit 0
fi

echo "✓ Found uv"
echo ""

# Check Python version
echo "Checking Python version..."
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Found Python $python_version"
echo ""

# Sync dependencies with uv
echo "Installing dependencies with uv..."
uv sync
echo "✓ Dependencies installed"
echo ""

# Setup environment
echo "Configure your API key:"
echo "  cp .env.example .env"
echo "  # Edit .env and add your Google API key"
echo ""

# Run instructions
echo "Run the agent with:"
echo "  uv run adk run agent.py          # CLI mode"
echo "  uv run adk run agent.py --web    # Web UI mode"
echo "  uv run python example.py         # Example script"
echo ""

echo "Get your API key from: https://aistudio.google.com/apikey"
echo ""
echo "=========================================="
