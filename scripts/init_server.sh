#!/usr/bin/env bash
# ==============================================================================
# SentinelRAG Production Server Initialization Script (Ubuntu 22.04 / 24.04 LTS)
# ==============================================================================
# Hardens OS, installs Docker Engine + Compose plugin, configures UFW firewall,
# prepares application directories, and sets up non-root deployment permissions.
#
# Usage:
#   chmod +x scripts/init_server.sh
#   sudo ./scripts/init_server.sh
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 1. Root verification
if [[ $EUID -ne 0 ]]; then
   log_error "This script must be executed as root (or with sudo)."
   exit 1
fi

log_info "Starting SentinelRAG Server Provisioning & Hardening..."

# 2. Update and upgrade base system packages
log_info "Updating system package repositories..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

# 3. Install core system utilities & prerequisites
log_info "Installing core system utilities (curl, gnupg, lsb-release, ufw, fail2ban)..."
apt-get install -y --no-install-recommends \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    ufw \
    fail2ban \
    htop \
    jq \
    git \
    tar \
    unzip

# 4. Configure UFW Firewall (Strict Default-Deny Policy)
log_info "Configuring UFW Firewall (Allowing only Ports 22, 80, 443)..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing

# Allow SSH with rate limiting to prevent brute force attacks
ufw limit 22/tcp comment 'SSH Rate Limited'

# Allow HTTP and HTTPS for Nginx Reverse Proxy & Certbot ACME Challenges
ufw allow 80/tcp comment 'HTTP (Let Encrypt ACME)'
ufw allow 443/tcp comment 'HTTPS (TLS Traffic)'

# Enable UFW
ufw --force enable
ufw status verbose
log_success "UFW Firewall configured and enabled successfully."

# 5. Configure Fail2ban for SSH Protection
log_info "Configuring fail2ban for SSH protection..."
systemctl enable fail2ban
systemctl restart fail2ban

# 6. Install Docker Engine and Docker Compose Plugin via Official Docker Repository
log_info "Installing official Docker Engine and Docker Compose plugin..."
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
fi

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 7. Configure Docker Daemon Security & Logging Defaults
log_info "Configuring Docker daemon security and logging limits..."
mkdir -p /etc/docker
cat <<EOF > /etc/docker/daemon.json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  },
  "live-restore": true,
  "userland-proxy": false,
  "no-new-privileges": true
}
EOF

systemctl daemon-reload
systemctl enable docker
systemctl restart docker
log_success "Docker Engine $(docker --version) and Compose $(docker compose version) installed."

# 8. Create Application Deployment Directories
APP_DIR="/opt/sentinel-rag"
log_info "Setting up application directory at ${APP_DIR}..."
mkdir -p "${APP_DIR}/nginx/conf.d"
mkdir -p "${APP_DIR}/certbot/conf"
mkdir -p "${APP_DIR}/certbot/www"
mkdir -p "${APP_DIR}/scripts"

# Grant non-root deployment user access if SUDO_USER is defined
if [ -n "${SUDO_USER:-}" ]; then
    usermod -aG docker "$SUDO_USER"
    chown -R "$SUDO_USER":"$SUDO_USER" "$APP_DIR"
    log_success "User '$SUDO_USER' added to 'docker' group and ownership granted to ${APP_DIR}."
fi

# 9. Verify Docker daemon operation
docker run --rm hello-world > /dev/null
log_success "Docker operational check passed."

echo -e "\n${GREEN}================================================================${NC}"
echo -e "${GREEN} SentinelRAG Host Server Provisioning Completed Successfully!  ${NC}"
echo -e "${GREEN} Next Steps:                                                   ${NC}"
echo -e " 1. Clone repository or copy docker-compose.prod.yml to ${APP_DIR}"
echo -e " 2. Populate ${APP_DIR}/.env with production secrets"
echo -e " 3. Configure Nginx and obtain Let's Encrypt certificates"
echo -e " 4. Run: docker compose -f docker-compose.prod.yml up -d"
echo -e "${GREEN}================================================================${NC}\n"
