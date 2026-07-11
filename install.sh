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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10
DOCKER_JUST_INSTALLED=false
INSTALL_MODE_LABEL="full setup"
SKIP_AI_ASSISTANT=false

# --- Helpers ---

setup_terminal_ui() {
    RED=""
    GREEN=""
    YELLOW=""
    CYAN=""
    DIM=""
    BOLD=""
    NC=""
    TERM_WIDTH=78
    USE_TTY_UI=false

    if command -v tput &>/dev/null; then
        TERM_WIDTH="$(tput cols 2>/dev/null || echo 78)"
        if [[ -z "$TERM_WIDTH" ]] || (( TERM_WIDTH < 60 )); then
            TERM_WIDTH=78
        elif (( TERM_WIDTH > 96 )); then
            TERM_WIDTH=96
        fi
    fi

    if [[ -z "${NO_COLOR:-}" ]] && [[ -t 1 ]] && [[ "${TERM:-}" != "dumb" ]] && command -v tput &>/dev/null; then
        local colors
        colors="$(tput colors 2>/dev/null || echo 0)"
        if [[ "$colors" =~ ^[0-9]+$ ]] && (( colors >= 8 )); then
            RED="$(tput setaf 1)"
            GREEN="$(tput setaf 2)"
            YELLOW="$(tput setaf 3)"
            CYAN="$(tput setaf 6)"
            BOLD="$(tput bold)"
            DIM="$(tput dim 2>/dev/null || true)"
            NC="$(tput sgr0)"
            USE_TTY_UI=true
        fi
    fi
}

repeat_char() {
    local char="${1:--}"
    local count="${2:-$TERM_WIDTH}"
    local output=""
    local i

    for ((i = 0; i < count; i++)); do
        output+="$char"
    done
    printf '%s' "$output"
}

rule() {
    local width="${1:-$TERM_WIDTH}"
    printf '%s\n' "${DIM}$(repeat_char "-" "$width")${NC}"
}

panel() {
    local title="$1"
    local body="$2"
    local width="${3:-76}"
    local title_text=" $title "
    local inner_width=$((width - 4))
    local line wrapped

    if [[ "$USE_TTY_UI" != "true" ]]; then
        printf '\n%s\n' "$title"
        printf '%s\n' "$body"
        return
    fi

    if (( width > TERM_WIDTH )); then
        width="$TERM_WIDTH"
        inner_width=$((width - 4))
    fi

    printf '\n%s╭─%s%s%s%s%s╮%s\n' \
        "$CYAN" "$BOLD" "$title_text" "$NC" "$CYAN" \
        "$(repeat_char "─" $((width - 3 - ${#title_text})))" "$NC"
    while IFS= read -r line; do
        if [[ -z "$line" ]]; then
            printf '%s│%s %-*s %s│%s\n' "$CYAN" "$NC" "$inner_width" "" "$CYAN" "$NC"
            continue
        fi
        while IFS= read -r wrapped; do
            printf '%s│%s %-*s %s│%s\n' "$CYAN" "$NC" "$inner_width" "$wrapped" "$CYAN" "$NC"
        done < <(printf '%s\n' "$line" | fold -s -w "$inner_width")
    done <<< "$body"
    printf '%s╰%s╯%s\n' "$CYAN" "$(repeat_char "─" $((width - 2)))" "$NC"
}

banner() {
    panel "Minos Subnet Installer" "Decentralized genomic variant calling on Bittensor SN107
Safe to re-run: completed steps are detected and reused." 76
    printf '  %-14s %s\n' "Mode" "$INSTALL_MODE_LABEL"
    printf '  %-14s %s\n' "Directory" "$SCRIPT_DIR"
    printf '  %-14s %s\n' "Python" "${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ required"
    printf '  %-14s %s\n' "Color" "$([[ -n "$NC" ]] && echo "enabled" || echo "plain text")"
    printf '\n'
}

section() {
    printf '\n%s◆%s %s%s%s\n' "$CYAN" "$NC" "$BOLD" "$1" "$NC"
    rule 76
}

status_line() {
    local symbol="$1"
    local color="$2"
    shift 2
    printf '  %s%s%s %s\n' "$color" "$symbol" "$NC" "$*"
}

info()  { status_line "•" "$CYAN" "$1"; }
ok()    { status_line "✓" "$GREEN" "$1"; }
warn()  { status_line "⚠" "$YELLOW" "$1"; }
fail()  { status_line "✗" "$RED" "$1" >&2; }
header() { section "$1"; }

usage() {
    cat <<'EOF'
Minos Subnet Installer

Usage:
  bash install.sh [OPTIONS]

Options:
  --fresh, --full       Redo the full setup even if an existing install is detected.
  --no-ai-assistant    Skip the optional Minos Miner AI Assistant prompt.
  --help, -h           Show this help.

Examples:
  bash install.sh
  bash install.sh --fresh
  bash install.sh --no-ai-assistant
  NO_COLOR=1 bash install.sh

What this does:
  - Checks OS, Python, Docker, Node/npm, and PM2.
  - Creates or reuses the Python virtual environment.
  - Installs Python dependencies.
  - Launches the interactive Minos setup wizard.
  - Optionally offers the Minos Miner AI Assistant setup.

Safety:
  The installer does not print wallet secrets, private keys, .env values, or
  model API keys. Optional assistant setup is public-memory/runtime setup only.
EOF
}

print_command() {
    printf '  %s%s%s\n' "$DIM" "$1" "$NC"
}

print_log_tail() {
    local log_file="$1"
    if [[ -f "$log_file" ]]; then
        tail -5 "$log_file" | sed 's/^/      /' >&2
    fi
}

run_quiet() {
    local message="$1"
    local log_file="$2"
    shift 2

    if [[ "$USE_TTY_UI" != "true" ]]; then
        info "$message"
        "$@"
        return
    fi

    : > "$log_file"
    "$@" >"$log_file" 2>&1 &
    local pid=$!
    local frames=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
    local i=0

    while kill -0 "$pid" 2>/dev/null; do
        printf '\r  %s%s%s %s' "$CYAN" "${frames[$((i % ${#frames[@]}))]}" "$NC" "$message"
        i=$((i + 1))
        sleep 0.12
    done

    if wait "$pid"; then
        printf '\r\033[K'
        ok "$message"
        return 0
    fi

    printf '\r\033[K'
    fail "$message"
    print_log_tail "$log_file"
    return 1
}

success_card() {
    local title="$1"
    local body="$2"
    if [[ "$USE_TTY_UI" == "true" ]]; then
        local saved_cyan="$CYAN"
        CYAN="$GREEN"
        panel "✓ $title" "$body" 76
        CYAN="$saved_cyan"
    else
        printf '\n%s\n%s\n\n' "$title" "$body"
    fi
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
            print_command "Install WSL2: https://learn.microsoft.com/en-us/windows/wsl/install"
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
            info "Installing Python via apt..."
            sudo apt-get update -qq
            sudo apt-get install -y python3 python3-venv python3-pip
            PYTHON_CMD="python3"
            ;;
        dnf|yum)
            info "Installing Python via $PKG_MANAGER..."
            sudo "$PKG_MANAGER" install -y python3 python3-pip python3-libs
            PYTHON_CMD="python3"
            ;;
        brew)
            if command -v brew &>/dev/null; then
                info "Installing Python via Homebrew..."
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

check_zstd() {
    # zstd is used by setup.py to extract the optional reference-data archive
    # (validators only). Per-file fallback works without it; this is a soft
    # dependency, install is best-effort.
    if command -v zstd &>/dev/null; then
        return
    fi

    info "Installing zstd (used for fast reference-data archive extract)..."
    case "$PKG_MANAGER" in
        apt)
            sudo apt-get update -qq 2>/dev/null
            sudo apt-get install -y -qq zstd 2>/dev/null && ok "zstd installed" || warn "zstd install failed; archive download will fall back to per-file"
            ;;
        dnf|yum)
            sudo "$PKG_MANAGER" makecache -q 2>/dev/null
            sudo "$PKG_MANAGER" install -y -q zstd 2>/dev/null && ok "zstd installed" || warn "zstd install failed; archive download will fall back to per-file"
            ;;
        brew) brew install zstd 2>/dev/null && ok "zstd installed" || warn "zstd install failed; archive download will fall back to per-file" ;;
        *) warn "zstd not present and package manager unknown; archive download will fall back to per-file" ;;
    esac
}


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
                exit 1
            fi

            info "Trying to start Docker..."
            if command -v systemctl &>/dev/null; then
                sudo systemctl start docker 2>/dev/null || true
            elif command -v service &>/dev/null; then
                sudo service docker start 2>/dev/null || true
            fi

            if docker info &>/dev/null; then
                ok "Docker daemon started"
                return
            fi
            fail "Docker is installed but the daemon is not running."
            print_command "Start it manually: sudo systemctl start docker"
            exit 1
        fi
    fi

    warn "Docker not found."

    case "$OS" in
        Linux)
            info "Installing Docker via the official Docker script..."
            info "Review source: https://get.docker.com"
            if ! command -v curl &>/dev/null; then
                info "Installing curl for Docker bootstrap..."
                case "$PKG_MANAGER" in
                    apt)
                        sudo apt-get update -qq
                        sudo apt-get install -y curl ca-certificates
                        ;;
                    dnf|yum)
                        sudo "$PKG_MANAGER" install -y curl ca-certificates
                        ;;
                    *)
                        fail "curl is required to download Docker."
                        print_command "Install curl and re-run: bash install.sh"
                        exit 1
                        ;;
                esac
            fi

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

            if ! docker info &>/dev/null; then
                if command -v systemctl &>/dev/null; then
                    sudo systemctl start docker 2>/dev/null || true
                elif command -v service &>/dev/null; then
                    sudo service docker start 2>/dev/null || true
                fi
            fi

            ok "Docker installed."
            DOCKER_JUST_INSTALLED=true
            ;;
        Darwin)
            fail "Docker Desktop is required on macOS."
            print_command "Download from: https://www.docker.com/products/docker-desktop/"
            print_command "Install it, start it, then re-run this script."
            exit 1
            ;;
    esac
}

# --- Virtual environment ---

setup_venv() {
    header "Setting up Python virtual environment"

    local venv_dir="${1:-$SCRIPT_DIR/.venv}"
    local venv_label
    if [[ "$venv_dir" == "$SCRIPT_DIR/.venv" ]]; then
        venv_label=".venv"
    else
        venv_label="$venv_dir"
    fi
    local managed_minosvm_venv=false
    if [[ "$venv_dir" == "/opt/minosvm_venv" ]]; then
        managed_minosvm_venv=true
    fi

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

    # If the virtual environment exists but bin/activate is missing, it's a
    # partial/failed creation. Repo-local .venv can be repaired; MinosVM's
    # managed /opt runtime should fail clearly rather than being deleted.
    if [[ -d "$venv_dir" ]] && [[ ! -f "$venv_dir/bin/activate" ]]; then
        if [[ "$managed_minosvm_venv" == "true" ]]; then
            fail "$venv_label exists but is incomplete."
            print_command "Run: bash install.sh --fresh"
            exit 1
        fi
        warn "$venv_label exists but is incomplete (previous install may have failed). Removing and recreating..."
        rm -rf "$venv_dir"
    fi

    if [[ -d "$venv_dir" && -f "$venv_dir/bin/activate" && ! -x "$venv_dir/bin/python" ]]; then
        if [[ "$managed_minosvm_venv" == "true" ]]; then
            fail "$venv_label is missing an executable Python."
            print_command "Run: bash install.sh --fresh"
            exit 1
        fi
        warn "$venv_label is missing an executable Python. Removing and recreating..."
        rm -rf "$venv_dir"
    fi

    if [[ -d "$venv_dir" ]]; then
        # Check venv Python version matches system Python
        local venv_py_ver expected_ver
        venv_py_ver="$("$venv_dir/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)"
        expected_ver="$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)"

        if [[ -n "$venv_py_ver" ]] && [[ "$venv_py_ver" != "$expected_ver" ]]; then
            if [[ "$managed_minosvm_venv" == "true" ]]; then
                fail "$venv_label uses Python $venv_py_ver but system has $expected_ver."
                print_command "Run: bash install.sh --fresh"
                exit 1
            fi
            warn "Existing $venv_label uses Python $venv_py_ver but system has $expected_ver."
            info "Recreating virtual environment with Python $expected_ver..."
            rm -rf "$venv_dir"
            "$PYTHON_CMD" -m venv "$venv_dir"
            ok "Virtual environment recreated with Python $expected_ver"
        else
            ok "Virtual environment exists at $venv_label (Python ${venv_py_ver:-unknown})"
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
        ok "Virtual environment created at $venv_label"
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

    info "Installing Python dependencies (includes the CPU build of PyTorch ~200 MB; a few minutes)."
    if ! run_quiet "Upgrading pip" "/tmp/minos-pip-upgrade.log" pip install --upgrade pip -q; then
        fail "pip upgrade failed. Check /tmp/minos-pip-upgrade.log for details."
        exit 1
    fi
    if ! run_quiet "Installing requirements.txt" "/tmp/minos-pip-requirements.log" pip install -r "$req_file" -q; then
        fail "pip install failed. Check the output above for details."
        if [[ "$PKG_MANAGER" == "apt" ]]; then
            fail "Common fix: sudo apt-get install python3-dev build-essential zlib1g-dev"
        fi
        exit 1
    fi

    ok "All dependencies installed."
}

# --- Node.js (prerequisite for PM2 and optional AI runtimes) ---
#
# Fresh Ubuntu cloud images don't ship Node/npm, and the distro's apt
# nodejs can be too old for PM2 and OpenClaw. NodeSource is the official
# upstream-managed apt repo that ships a current Node release for supported
# Ubuntu/Debian releases. macOS gets node via Homebrew.

install_node() {
    header "Node.js (for PM2)"

    if command -v node &>/dev/null; then
        local major
        major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
        if [[ "$major" =~ ^[0-9]+$ ]] && (( major >= 22 )); then
            if command -v npm &>/dev/null; then
                ok "Node $(node -v) (npm $(npm -v 2>/dev/null || echo '?'))"
                return
            fi
            warn "Node $(node -v) found, but npm is missing. Repairing Node/npm so PM2 can be installed."
        else
            warn "Node $(node -v) is too old for the current Minos tooling; will upgrade."
        fi
    fi

    # Node install is best-effort — PM2 is optional, and an existing miner
    # running `update_only` shouldn't have their dependency upgrade aborted
    # just because the network blocked NodeSource. Every command below uses
    # ||true / explicit guards so set -eo pipefail can't crash the outer
    # install path mid-step.
    local nodelog="/tmp/minos-node-install.log"
    : > "$nodelog"

    case "$PKG_MANAGER" in
        apt)
            info "Updating apt package index for Node/npm..."
            if ! sudo apt-get update -qq >>"$nodelog" 2>&1; then
                warn "apt-get update failed while preparing Node/npm. Last log lines:"
                print_log_tail "$nodelog"
            fi

            info "Adding NodeSource repo for Node 22.x..."
            if ! sudo apt-get install -y -qq curl ca-certificates gnupg >>"$nodelog" 2>&1; then
                warn "Failed to install NodeSource prereqs (curl/gpg). See $nodelog. Skipping Node install."
                return
            fi
            if curl -fsSL https://deb.nodesource.com/setup_22.x 2>>"$nodelog" | sudo bash - >>"$nodelog" 2>&1; then
                if ! sudo apt-get install -y nodejs >>"$nodelog" 2>&1; then
                    warn "apt-get install nodejs failed. Last log lines:"
                    print_log_tail "$nodelog"
                    return
                fi
            else
                warn "NodeSource setup_22.x failed (network/proxy/sudo). Last lines:"
                print_log_tail "$nodelog"
                warn "Falling back to distro nodejs (may be too old for optional AI runtimes)."
                sudo apt-get install -y nodejs npm >>"$nodelog" 2>&1 \
                    || warn "Distro nodejs install also failed. See $nodelog."
            fi

            if command -v node &>/dev/null && ! command -v npm &>/dev/null; then
                warn "Node is present but npm is still missing. Installing distro npm package..."
                sudo apt-get install -y npm >>"$nodelog" 2>&1 \
                    || warn "npm install failed. See $nodelog."
            fi
            ;;
        dnf|yum)
            info "Adding NodeSource repo for Node 22.x..."
            if curl -fsSL https://rpm.nodesource.com/setup_22.x 2>>"$nodelog" | sudo bash - >>"$nodelog" 2>&1; then
                sudo "$PKG_MANAGER" install -y nodejs >>"$nodelog" 2>&1 \
                    || { warn "nodejs install failed (see $nodelog)"; return; }
            else
                warn "NodeSource setup failed; falling back to distro nodejs."
                sudo "$PKG_MANAGER" install -y nodejs npm >>"$nodelog" 2>&1 \
                    || warn "Distro nodejs install failed (see $nodelog)"
            fi
            if command -v node &>/dev/null && ! command -v npm &>/dev/null; then
                warn "Node is present but npm is still missing. Installing npm package..."
                sudo "$PKG_MANAGER" install -y npm >>"$nodelog" 2>&1 \
                    || warn "npm install failed. See $nodelog."
            fi
            ;;
        brew)
            if ! brew install node >>"$nodelog" 2>&1; then
                warn "brew install node failed. Last log lines:"
                print_log_tail "$nodelog"
            fi
            ;;
        *)
            warn "Unknown package manager '${PKG_MANAGER:-?}'; install Node 22+ manually then re-run install.sh"
            return
            ;;
    esac

    if command -v node &>/dev/null && command -v npm &>/dev/null; then
        ok "Node $(node -v) and npm $(npm -v) installed"
    elif command -v node &>/dev/null; then
        warn "Node $(node -v) is installed, but npm is still missing (see $nodelog). PM2 will be skipped."
    else
        warn "Node install did not produce a 'node' binary on PATH (see $nodelog). PM2 will be skipped."
    fi
}

# --- PM2 (when npm is available) ---

install_pm2() {
    header "PM2 (process manager)"

    if command -v pm2 &>/dev/null; then
        ok "PM2 already installed ($(pm2 -v 2>/dev/null | head -1 || echo ok))"
        return
    fi

    if ! command -v npm &>/dev/null; then
        warn "npm still missing after install_node — skipping PM2."
        return
    fi

    # Snap-installed Node writes its global prefix to a read-only location;
    # NodeSource installs to /usr/lib/node_modules (root-owned). Either way
    # `npm install -g` needs sudo unless npm's prefix is user-writable
    # (nvm, ~/.local prefix, etc.). Detecting writability is cheaper and
    # more accurate than guessing from `whoami`.
    local npm_prefix prefix_dir prefix_bin
    local -a install_cmd
    npm_prefix="$(npm config get prefix 2>/dev/null || true)"
    prefix_dir="$npm_prefix/lib/node_modules"
    prefix_bin="$npm_prefix/bin"
    if [[ -w "$prefix_dir" ]] || [[ "$(id -u)" == "0" ]]; then
        install_cmd=(npm install -g pm2)
    else
        install_cmd=(sudo npm install -g pm2)
    fi

    local log="/tmp/minos-pm2-install.log"
    local try
    for try in 1 2 3; do
        info "Installing PM2 (attempt $try/3): ${install_cmd[*]}"
        if "${install_cmd[@]}" >"$log" 2>&1; then
            hash -r 2>/dev/null || true
            if command -v pm2 &>/dev/null; then
                ok "PM2 installed ($(pm2 -v 2>/dev/null || echo ok))"
                return
            fi
            if [[ -n "$prefix_bin" && -x "$prefix_bin/pm2" ]]; then
                export PATH="$prefix_bin:$PATH"
                if command -v pm2 &>/dev/null; then
                    ok "PM2 installed ($(pm2 -v 2>/dev/null || echo ok))"
                    warn "PM2 was installed under $prefix_bin. Add it to PATH if future shells cannot find pm2:"
                    warn "  export PATH=\"$prefix_bin:\$PATH\""
                    return
                fi
                warn "PM2 installed at $prefix_bin/pm2, but that directory is not on PATH."
                warn "Add it to PATH, then verify with: pm2 -v"
                return
            fi
            warn "npm reported PM2 installed, but no pm2 binary was found on PATH."
            warn "Last npm log lines:"
            print_log_tail "$log"
            return
        fi
        warn "PM2 install attempt $try failed (see $log for npm output)"
        if (( try < 3 )); then
            sleep $((try * 5))
        fi
    done
    warn "PM2 install failed after 3 tries. To debug:"
    warn "  cat $log"
    warn "  ${install_cmd[*]}          # run manually"
    warn "PM2 is optional — the miner/validator runs fine without it."
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

show_optional_ai_assistant_next_step() {
    local mode="${1:-prompt}"
    local prompt_script="$SCRIPT_DIR/scripts/prompt_ai_assistant.sh"
    if [[ "$SKIP_AI_ASSISTANT" == "true" ]]; then
        return
    fi
    if [[ -f "$prompt_script" ]]; then
        if [[ "$mode" == "prompt" ]]; then
            bash "$prompt_script" --prompt --once --default y --role miner || true
        else
            bash "$prompt_script" --print --role miner || true
        fi
    fi
}

# --- Update mode (existing install detected) ---

update_only() {
    header "Existing installation detected — updating"

    local venv_dir="${1:-$SCRIPT_DIR/.venv}"
    local venv_py_ver expected_ver

    if [[ ! -f "$venv_dir/bin/activate" || ! -x "$venv_dir/bin/python" ]]; then
        fail "Existing install points to an incomplete virtual environment: $venv_dir"
        print_command "Run: bash install.sh --fresh"
        exit 1
    fi

    venv_py_ver="$("$venv_dir/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)"
    expected_ver="$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)"
    if [[ -n "$venv_py_ver" && -n "$expected_ver" && "$venv_py_ver" != "$expected_ver" ]]; then
        fail "$venv_dir uses Python $venv_py_ver, but the current system Python is $expected_ver."
        print_command "Run: bash install.sh --fresh"
        exit 1
    fi

    # Activate venv
    # shellcheck disable=SC1091
    source "$venv_dir/bin/activate"
    ok "Activated $venv_dir ($(python --version 2>&1))"

    # Update pip deps (only installs new/changed packages)
    info "Checking for dependency updates..."
    pip install --upgrade pip -q
    pip install -r "$SCRIPT_DIR/requirements.txt" -q
    ok "Dependencies up to date"

    install_node
    install_pm2

    # Run migration + reference data download via setup.py --update-data-only
    info "Checking reference data..."
    python "$SCRIPT_DIR/setup.py" --update-data-only
}

# --- Main ---

main() {
    setup_terminal_ui
    local fresh=false
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --fresh|--full)
                fresh=true
                shift
                ;;
            --no-ai-assistant)
                SKIP_AI_ASSISTANT=true
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                echo "Unknown option: $1" >&2
                echo "Run: bash install.sh --help" >&2
                exit 1
                ;;
        esac
    done

    local venv_dir="$SCRIPT_DIR/.venv"
    local env_file="$SCRIPT_DIR/.env"

    # Reuse MinosVM's preinstalled runtime when present. Existing installs still
    # require .env for update mode; fresh MinosVM first runs reuse /opt instead
    # of creating a second repo-local .venv.
    local minosvm_venv="/opt/minosvm_venv"
    if [[ "$fresh" == "false" ]] && [[ -f "$minosvm_venv/bin/activate" && -x "$minosvm_venv/bin/python" ]]; then
        venv_dir="$minosvm_venv"
    fi
    if [[ "$fresh" == "false" ]] && [[ -f "$venv_dir/bin/activate" ]] && [[ -f "$env_file" ]]; then
        INSTALL_MODE_LABEL="update existing install"
        banner
        detect_os
        check_python
        check_zstd
        check_docker
        update_only "$venv_dir"
        show_optional_ai_assistant_next_step "print"
        success_card "Update complete" "Run bash install.sh --fresh to redo the full setup."
        exit 0
    fi

    # Full install
    INSTALL_MODE_LABEL="full setup"
    banner
    detect_os
    check_python
    check_zstd
    check_docker
    setup_venv "$venv_dir"
    install_deps
    install_node
    install_pm2
    launch_wizard
    show_optional_ai_assistant_next_step "prompt"
    success_card "Install finished" "Your Minos node setup flow is complete. Use bash start-miner.sh or pm2-miner.sh to run the miner."
}

main "$@"
