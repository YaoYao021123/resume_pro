#!/usr/bin/env bash
set -euo pipefail

# ─── Config ──────────────────────────────────────────
REMOTE_USER="${DEPLOY_USER:-root}"
REMOTE_HOST="${DEPLOY_HOST:?Set DEPLOY_HOST env var (e.g. 1.2.3.4)}"
REMOTE_DIR="${DEPLOY_DIR:-/opt/resume}"
SSH_KEY="${DEPLOY_SSH_KEY:-~/.ssh/id_rsa}"
SSH="ssh -i $SSH_KEY $REMOTE_USER@$REMOTE_HOST"

usage() {
    cat <<EOF
Usage: $0 <command>

Commands:
  setup    First-time ECS setup (install Docker, open firewall)
  deploy   Sync code and (re)start containers
  logs     Tail container logs
  backup   Backup data & output volumes to local machine
EOF
    exit 1
}

# ─── setup ───────────────────────────────────────────
cmd_setup() {
    echo "==> Setting up ECS at $REMOTE_HOST ..."
    $SSH bash -s <<'REMOTE'
set -euo pipefail

# Docker
if ! command -v docker &>/dev/null; then
    echo "Installing Docker ..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
fi

# Docker Compose plugin
if ! docker compose version &>/dev/null; then
    echo "Installing Docker Compose plugin ..."
    apt-get update && apt-get install -y docker-compose-plugin
fi

# Firewall (ufw)
if command -v ufw &>/dev/null; then
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
fi

echo "==> Setup complete"
REMOTE
}

# ─── deploy ──────────────────────────────────────────
cmd_deploy() {
    echo "==> Syncing code to $REMOTE_HOST:$REMOTE_DIR ..."
    rsync -avz --delete \
        --exclude '.git' \
        --exclude 'output/' \
        --exclude '.trash/' \
        --exclude '.env.production' \
        --exclude 'nginx/ssl/' \
        --exclude 'tests/' \
        --exclude 'extension/' \
        --exclude '.claude/' \
        --exclude '.playwright-mcp/' \
        --exclude '__pycache__/' \
        -e "ssh -i $SSH_KEY" \
        . "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"

    echo "==> Building and starting containers ..."
    $SSH "cd $REMOTE_DIR && docker compose build && docker compose up -d"

    echo "==> Deploy complete. Checking status ..."
    $SSH "cd $REMOTE_DIR && docker compose ps"
}

# ─── logs ────────────────────────────────────────────
cmd_logs() {
    $SSH "cd $REMOTE_DIR && docker compose logs -f --tail=100"
}

# ─── backup ──────────────────────────────────────────
cmd_backup() {
    local ts
    ts=$(date +%Y%m%d_%H%M%S)
    local backup_dir="backups/$ts"
    mkdir -p "$backup_dir"

    echo "==> Backing up data volume ..."
    $SSH "docker run --rm -v resume-data:/data -v /tmp:/backup alpine tar czf /backup/resume-data.tar.gz -C /data ."
    scp -i "$SSH_KEY" "$REMOTE_USER@$REMOTE_HOST:/tmp/resume-data.tar.gz" "$backup_dir/"

    echo "==> Backing up output volume ..."
    $SSH "docker run --rm -v resume-output:/data -v /tmp:/backup alpine tar czf /backup/resume-output.tar.gz -C /data ."
    scp -i "$SSH_KEY" "$REMOTE_USER@$REMOTE_HOST:/tmp/resume-output.tar.gz" "$backup_dir/"

    echo "==> Backup saved to $backup_dir/"
}

# ─── main ────────────────────────────────────────────
[[ $# -lt 1 ]] && usage
case "$1" in
    setup)  cmd_setup  ;;
    deploy) cmd_deploy ;;
    logs)   cmd_logs   ;;
    backup) cmd_backup ;;
    *)      usage      ;;
esac
