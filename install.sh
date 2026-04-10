#!/usr/bin/env bash
#
# Minos Subnet Installer
# Bootstraps system dependencies then launches the interactive setup wizard.
#
# Usage: ./install.sh
#

# Require bash — BASH_SOURCE and other features won't work with sh/dash
if [ -z "${BASH_VERSION:-}" ]; then
    echo "Error: This script must be run with bash. Use: bash install.sh"
    exit 1
fi

set -eo pipefail
trap 'fail "Unexpected error on line $LINENO. Re-run with: bash -x install.sh for debug output."' ERR

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10
DOCKER_JUST_INSTALLED=false

# --- Helpers ---

info()  { echo -e "${CYAN}[INFO]${NC}  $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $1"; }
header() { echo -e "\n${BOLD}$1${NC}"; echo "────────────────────────────────────────"; }

ask_yes_no() {
    local prompt="$1"
    local default="${2:-y}"
    local yn
    if [[ "$default" == "y" ]]; then
        read -rp "$(echo -e "${BOLD}$prompt [Y/n]:${NC} ")" yn
        yn="${yn:-y}"
    else
        read -rp "$(echo -e "${BOLD}$prompt [y/N]:${NC} ")" yn
        yn="${yn:-n}"
    fi
    [[ "$yn" =~ ^[Yy] ]]
}

# Check if a Python command meets the minimum version requirement
_python_meets_version() {
    local cmd="$1"
    local ver major minor
    ver="$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)"
    major="$(echo "$ver" | cut -d. -f1)"
    minor="$(echo "$ver" | cut -d. -f2)"
    [[ -n "$major" ]] && [[ -n "$minor" ]] && \
       ( [[ "$major" -gt "$MIN_PYTHON_MAJOR" ]] || \
         ( [[ "$major" -eq "$MIN_PYTHON_MAJOR" ]] && [[ "$minor" -ge "$MIN_PYTHON_MINOR" ]] ) )
}

# --- Detect OS and package manager ---

detect_os() {
    header "Detecting system"

    OS="$(uname -s)"
    ARCH="$(uname -m)"
    PKG_MANAGER=""

    case "$OS" in
        Linux)
            if command -v apt-get &>/dev/null; then
                PKG_MANAGER="apt"
            elif command -v dnf &>/dev/null; then
                PKG_MANAGER="dnf"
            elif command -v yum &>/dev/null; then
                PKG_MANAGER="yum"
            fi
            ok "Linux ($ARCH) — package manager: ${PKG_MANAGER:-none detected}"
            ;;
        Darwin)
            PKG_MANAGER="brew"
            if ! command -v brew &>/dev/null; then
                warn "Homebrew not found. Some installs may require it."
                warn "Install from: https://brew.sh"
            fi
            ok "macOS ($ARCH)"
            ;;
        MINGW*|MSYS*|CYGWIN*)
            fail "Windows detected. Please use WSL2 (Windows Subsystem for Linux)."
            echo "  Install WSL2: https://learn.microsoft.com/en-us/windows/wsl/install"
            exit 1
            ;;
        *)
            fail "Unsupported OS: $OS"
            exit 1
            ;;
    esac
}

# --- Python check/install ---

check_python() {
    header "Checking Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+"

    local py_cmd=""
    for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            if _python_meets_version "$cmd"; then
                py_cmd="$cmd"
                break
            fi
        fi
    done

    if [[ -n "$py_cmd" ]]; then
        PYTHON_CMD="$py_cmd"
        ok "Python $("$PYTHON_CMD" --version 2>&1 | awk '{print $2}') found at $(command -v "$PYTHON_CMD")"
        return
    fi

    warn "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ not found."

    case "$PKG_MANAGER" in
        apt)
            if ask_yes_no "Install Python via apt?"; then
                sudo apt-get update -qq
                sudo apt-get install -y python3 python3-venv python3-pip
                PYTHON_CMD="python3"
            fi
            ;;
        dnf|yum)
            if ask_yes_no "Install Python via $PKG_MANAGER?"; then
                sudo "$PKG_MANAGER" install -y python3 python3-pip python3-libs
                PYTHON_CMD="python3"
            fi
            ;;
        brew)
            if command -v brew &>/dev/null && ask_yes_no "Install Python via Homebrew?"; then
                brew install python@3.12
                # Re-resolve: brew-installed Python may not be the default python3
                if command -v python3.12 &>/dev/null; then
                    PYTHON_CMD="python3.12"
                elif command -v python3 &>/dev/null; then
                    PYTHON_CMD="python3"
                else
                    PYTHON_CMD="$(brew --prefix python@3.12)/bin/python3.12"
                fi
            fi
            ;;
    esac

    if [[ -z "${PYTHON_CMD:-}" ]]; then
        fail "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ is required. Install it and re-run this script."
        exit 1
    fi

    # Verify the installed Python actually meets the version requirement
    if ! _python_meets_version "$PYTHON_CMD"; then
        local installed_ver
        installed_ver="$("$PYTHON_CMD" --version 2>&1)"
        fail "Installed $installed_ver does not meet the ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ requirement."
        if [[ "$PKG_MANAGER" == "apt" ]]; then
            fail "Try: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv"
        fi
        exit 1
    fi

    ok "Python installed: $("$PYTHON_CMD" --version 2>&1)"
}

# --- Docker check/install ---

check_docker() {
    header "Checking Docker"

    if command -v docker &>/dev/null; then
        local docker_ver
        docker_ver="$(docker --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"

        # Timeout docker info to avoid hanging when Docker Desktop is installed but not running
        # (docker info blocks indefinitely on macOS if daemon socket exists but isn't responding)
        info "Checking Docker daemon (up to 10s)..."
        local docker_ok=false
        docker info &>/dev/null &
        local docker_pid=$!
        for _i in $(seq 1 10); do
            if ! kill -0 "$docker_pid" 2>/dev/null; then
                # Process finished — check exit code
                if wait "$docker_pid" 2>/dev/null; then
                    docker_ok=true
                fi
                break
            fi
            sleep 1
        done
        # Still running after 10s — kill it; otherwise collect exit code
        if kill -0 "$docker_pid" 2>/dev/null; then
            kill "$docker_pid" 2>/dev/null
            wait "$docker_pid" 2>/dev/null || true
        else
            # Process exited during last sleep — collect its exit code
            if wait "$docker_pid" 2>/dev/null; then
                docker_ok=true
            fi
        fi

        if $docker_ok; then
            ok "Docker $docker_ver (daemon running)"
            return
        else
            warn "Docker $docker_ver installed but daemon is not running."
            if [[ "$OS" == "Darwin" ]]; then
                warn "Start Docker Desktop before running your miner/validator."
            else
                warn "Start with: sudo systemctl start docker"
            fi
            if ask_yes_no "Continue setup without Docker?" "n"; then
                warn "Continuing — Docker must be running before launching your node."
                return
            fi
            exit 1
        fi
    fi

    warn "Docker not found."

    case "$OS" in
        Linux)
            if ask_yes_no "Install Docker via official script (get.docker.com)?"; then
                if ! command -v curl &>/dev/null; then
                    fail "curl is required to download the Docker install script."
                    if [[ -n "$PKG_MANAGER" ]]; then
                        fail "Install it with: sudo $PKG_MANAGER install curl"
                    else
                        fail "Please install curl and re-run this script."
                    fi
                    exit 1
                fi
                info "This will download and run Docker's official install script with sudo."
                info "Review it at: https://get.docker.com"
                info "Downloading Docker install script..."
                curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
                sudo sh /tmp/get-docker.sh
                rm -f /tmp/get-docker.sh

                # Add current user to docker group
                local current_user="${USER:-$(whoami)}"
                if ! groups "$current_user" 2>/dev/null | grep -q docker; then
                    sudo usermod -aG docker "$current_user"
                    warn "Added $current_user to docker group. You may need to log out and back in."
                    warn "Or run: newgrp docker"
                fi

                ok "Docker installed and started."
                DOCKER_JUST_INSTALLED=true
            else
                fail "Docker is required. Install it and re-run this script."
                exit 1
            fi
            ;;
        Darwin)
            fail "Docker Desktop is required on macOS."
            echo "  Download from: https://www.docker.com/products/docker-desktop/"
            echo "  Install it, start it, then re-run this script."
            exit 1
            ;;
    esac
}

# --- Virtual environment ---

setup_venv() {
    header "Setting up Python virtual environment"

    local venv_dir="$SCRIPT_DIR/.venv"

    # On apt systems, ensure the version-specific venv package is installed before any
    # venv creation attempt. Ubuntu ships python3.12 without python3.12-venv by default;
    # 'python3 -m venv --help' succeeds but actual creation fails with "ensurepip is not available".
    # This runs once here so both new-venv and recreate-venv paths are covered.
    if [[ "$PKG_MANAGER" == "apt" ]]; then
        local py_ver
        py_ver="$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)"
        local venv_pkg="python${py_ver}-venv"
        if ! dpkg -s "$venv_pkg" &>/dev/null; then
            info "Installing $venv_pkg (required for virtual environment)..."
            sudo apt-get update -qq
            sudo apt-get install -y "$venv_pkg" || {
                fail "Could not install $venv_pkg. Try: sudo apt-get install $venv_pkg"
                exit 1
            }
        fi
    fi

    # If .venv exists but bin/activate is missing, it's a partial/failed creation — clean it up.
    if [[ -d "$venv_dir" ]] && [[ ! -f "$venv_dir/bin/activate" ]]; then
        warn ".venv exists but is incomplete (previous install may have failed). Removing and recreating..."
        rm -rf "$venv_dir"
    fi

    if [[ -d "$venv_dir" ]]; then
        # Check venv Python version matches system Python
        local venv_py_ver expected_ver
        venv_py_ver="$("$venv_dir/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)"
        expected_ver="$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)"

        if [[ -n "$venv_py_ver" ]] && [[ "$venv_py_ver" != "$expected_ver" ]]; then
            warn "Existing .venv uses Python $venv_py_ver but system has $expected_ver."
            if ask_yes_no "Recreate virtual environment with Python $expected_ver?"; then
                rm -rf "$venv_dir"
                info "Creating virtual environment..."
                "$PYTHON_CMD" -m venv "$venv_dir"
                ok "Virtual environment recreated with Python $expected_ver"
            else
                ok "Keeping existing .venv (Python $venv_py_ver)"
            fi
        else
            ok "Virtual environment exists at .venv/ (Python ${venv_py_ver:-unknown})"
        fi
    else
        # Verify venv module is available before creating
        if ! "$PYTHON_CMD" -m venv --help &>/dev/null; then
            fail "Python venv module not available."
            if [[ "$PKG_MANAGER" == "dnf" ]] || [[ "$PKG_MANAGER" == "yum" ]]; then
                fail "Install it with: sudo $PKG_MANAGER install python3-devel"
            else
                fail "Install the python3-venv package for your distro."
            fi
            exit 1
        fi
        info "Creating virtual environment..."
        "$PYTHON_CMD" -m venv "$venv_dir"
        ok "Virtual environment created at .venv/"
    fi

    # shellcheck disable=SC1091
    source "$venv_dir/bin/activate"
    ok "Activated .venv ($(python --version 2>&1))"
}

# --- Pip dependencies ---

install_deps() {
    header "Installing Python dependencies"

    local req_file="$SCRIPT_DIR/requirements.txt"

    if [[ ! -f "$req_file" ]]; then
        fail "requirements.txt not found at $req_file"
        exit 1
    fi

    info "Installing Python dependencies (includes PyTorch ~2 GB, may take 5-15 minutes)..."
    pip install --upgrade pip
    if ! pip install -r "$req_file"; then
        fail "pip install failed. Check the output above for details."
        if [[ "$PKG_MANAGER" == "apt" ]]; then
            fail "Common fix: sudo apt-get install python3-dev build-essential zlib1g-dev"
        fi
        exit 1
    fi

    ok "All dependencies installed."
}

# --- Launch wizard ---

launch_wizard() {
    header "Launching setup wizard"

    local wizard="$SCRIPT_DIR/setup.py"

    if [[ ! -f "$wizard" ]]; then
        fail "setup.py not found at $wizard"
        exit 1
    fi

    if [[ "${DOCKER_JUST_INSTALLED:-false}" == "true" ]] && [[ "$OS" == "Linux" ]]; then
        # Docker was just installed — the docker group is not yet active in this shell.
        # sg runs a command with the specified group active, no logout needed.
        info "Activating docker group for this session..."
        if command -v sg &>/dev/null; then
            sg docker -c "python \"$wizard\""
        else
            warn "sg not available; docker commands may fail. Run 'newgrp docker' if wizard blocks on Docker."
            python "$wizard"
        fi
    else
        python "$wizard"
    fi
}

# --- Update mode (existing install detected) ---

update_only() {
    header "Existing installation detected — updating"

    local venv_dir="$SCRIPT_DIR/.venv"

    # Activate venv
    # shellcheck disable=SC1091
    source "$venv_dir/bin/activate"
    ok "Activated .venv ($(python --version 2>&1))"

    # Update pip deps (only installs new/changed packages)
    info "Checking for dependency updates..."
    pip install --upgrade pip -q
    pip install -r "$SCRIPT_DIR/requirements.txt" -q
    ok "Dependencies up to date"

    # Run migration + reference data download via setup.py --update-only
    info "Checking reference data..."
    python "$SCRIPT_DIR/setup.py" --update-data-only
}

# --- Main ---

main() {
    echo ""
    echo -e "${BOLD}${CYAN}Minos Subnet Installer${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    local fresh=false
    if [[ "${1:-}" == "--fresh" ]] || [[ "${1:-}" == "--full" ]]; then
        fresh=true
    fi

    local venv_dir="$SCRIPT_DIR/.venv"
    local env_file="$SCRIPT_DIR/.env"

    # Detect existing install (local .venv or MinosVM /opt/minosvm_venv)
    local minosvm_venv="/opt/minosvm_venv"
    if [[ "$fresh" == "false" ]] && [[ -f "$minosvm_venv/bin/activate" ]] && [[ -f "$env_file" ]]; then
        venv_dir="$minosvm_venv"
    fi
    if [[ "$fresh" == "false" ]] && [[ -f "$venv_dir/bin/activate" ]] && [[ -f "$env_file" ]]; then
        detect_os
        check_python
        check_docker
        update_only
        echo ""
        echo -e "${GREEN}Update complete.${NC} Run with ${BOLD}--fresh${NC} to redo full setup."
        exit 0
    fi

    # Full install
    detect_os
    check_python
    check_docker
    setup_venv
    install_deps
    launch_wizard
}

main "$@"
