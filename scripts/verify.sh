#!/usr/bin/env bash
# Minos Subnet 107 — Environment Verification
# Checks that all prerequisites are installed and configured.
# Usage: bash scripts/verify.sh [--miner|--validator]
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; NC='\033[0m'
PASS=0; WARN=0; FAIL=0

pass() { echo -e "  ${GREEN}[OK]${NC} $1"; PASS=$((PASS+1)); }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $1"; WARN=$((WARN+1)); }
fail() { echo -e "  ${RED}[FAIL]${NC} $1"; FAIL=$((FAIL+1)); }

ROLE="${1:---miner}"

# Resolve project root and activate venv if available
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="python3"
if [[ -f "$PROJECT_DIR/.venv/bin/python3" ]]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [[ -f "$PROJECT_DIR/.venv/bin/python" ]]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
fi

echo "=================================="
echo "  Minos SN107 — Setup Verification"
echo "  Role: ${ROLE#--}"
echo "=================================="
echo ""

# --- Python ---
echo "Python:"
if [[ -x "$PYTHON" ]] || command -v "$PYTHON" &>/dev/null; then
    pass "$PYTHON ($($PYTHON --version 2>&1 | awk '{print $2}'))"
else
    fail "python3 not found"
fi

for pkg in bittensor pysam boto3 dotenv; do
    if $PYTHON -c "import ${pkg}" 2>/dev/null; then
        pass "$PYTHON -c 'import ${pkg}'"
    else
        pip_name="${pkg}"
        [[ "$pkg" == "dotenv" ]] && pip_name="python-dotenv"
        fail "Missing Python package: ${pkg} (pip install ${pip_name})"
    fi
done

# --- Docker (required) ---
echo ""
echo "Docker:"
if command -v docker &>/dev/null; then
    pass "docker installed ($(docker --version 2>&1 | head -1))"
else
    fail "Docker not installed — required for variant calling"
    echo "       Install: https://docs.docker.com/get-docker/"
fi

if docker info &>/dev/null; then
    pass "Docker daemon running"
else
    fail "Docker daemon not running — start Docker and try again"
fi

# --- Docker images ---
echo ""
echo "Docker images:"

MINER_IMAGES=(
    "broadinstitute/gatk:4.5.0.0"
    "google/deepvariant:1.5.0"
    "staphb/freebayes:1.3.7"
    "quay.io/biocontainers/bcftools:1.20--h8b25389_0"
    "quay.io/biocontainers/samtools:1.20--h50ea8bc_0"
)
VALIDATOR_IMAGES=(
    "genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2"
)

check_images() {
    for img in "$@"; do
        if docker image inspect "$img" &>/dev/null; then
            short="${img##*/}"
            pass "$short"
        else
            short="${img##*/}"
            warn "$short not pulled (docker pull $img)"
        fi
    done
}

if [[ "$ROLE" == "--miner" ]]; then
    check_images "${MINER_IMAGES[@]}"
    echo -e "  ${YELLOW}Note:${NC} You only need the image for your chosen variant caller."
elif [[ "$ROLE" == "--validator" ]]; then
    check_images "${VALIDATOR_IMAGES[@]}"
    check_images "${MINER_IMAGES[@]}"
fi

# --- Wallet ---
echo ""
echo "Bittensor wallet:"
WALLET_PATH="$HOME/.bittensor/wallets"
if [ -d "$WALLET_PATH" ] && [ "$(ls -A "$WALLET_PATH" 2>/dev/null)" ]; then
    wallets=$(ls "$WALLET_PATH" 2>/dev/null | head -5)
    pass "Wallet(s) found: $wallets"
else
    warn "No wallets in $WALLET_PATH — create with: btcli wallet create"
fi

# --- .env ---
echo ""
echo "Configuration:"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VANET_DIR="$(dirname "$SCRIPT_DIR")"

if [ -f "$VANET_DIR/.env" ]; then
    pass ".env found"
elif [ -f "$VANET_DIR/.env.miner.example" ]; then
    warn "No .env — copy from .env.${ROLE#--}.example and fill in values"
else
    warn "No .env file found"
fi

# --- Summary ---
echo ""
echo "=================================="
echo -e "  ${GREEN}Passed: $PASS${NC}  ${YELLOW}Warnings: $WARN${NC}  ${RED}Failed: $FAIL${NC}"
echo "=================================="

if [ $FAIL -gt 0 ]; then
    echo -e "${RED}Fix the failures above before running your ${ROLE#--}.${NC}"
    exit 1
elif [ $WARN -gt 0 ]; then
    echo -e "${YELLOW}Warnings are non-blocking but should be addressed.${NC}"
    exit 0
else
    echo -e "${GREEN}All checks passed — ready to run!${NC}"
    exit 0
fi
