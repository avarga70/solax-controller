#!/usr/bin/env bash
set -euo pipefail

REMOTE=solman
REMOTE_DIR="/home/avarga/AI/solax"
WEB_DIR="/var/www/html/solax"
VENV="$REMOTE_DIR/venv"

DEPLOY_FILES=(
    solax_controller.py
    solax_poller.py
    download_pnd.py
    config.env.example
    requirements.txt
)

RESTART=0
for arg in "$@"; do [[ "$arg" == "--restart" ]] && RESTART=1; done

echo "Deploying to $REMOTE..."
ssh "$REMOTE" "mkdir -p $REMOTE_DIR"
rsync -av --files-from=<(printf '%s\n' "${DEPLOY_FILES[@]}") \
    . "$REMOTE:$REMOTE_DIR/"

echo "Installing Python dependencies..."
ssh "$REMOTE" "$VENV/bin/pip install -q -r $REMOTE_DIR/requirements.txt"

if [[ -d solar_web ]]; then
    echo "Deploying web UI..."
    ssh "$REMOTE" "sudo mkdir -p $WEB_DIR && sudo chown avarga:www-data $WEB_DIR"
    rsync -av solar_web/ "$REMOTE:$WEB_DIR/"
fi

if [[ $RESTART -eq 1 ]]; then
    echo "Restarting services..."
    ssh "$REMOTE" "sudo systemctl restart solax-controller solax-poller 2>/dev/null || true"
fi

echo "Deploy complete."
echo ""
echo "Quick start on $REMOTE:"
echo "  sudo mkdir -p /etc/solax"
echo "  sudo cp $REMOTE_DIR/config.env.example /etc/solax/config.env"
echo "  # Edit /etc/solax/config.env with your inverter IP and DB password"
