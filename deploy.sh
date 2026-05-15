#!/usr/bin/env bash
set -euo pipefail

REMOTE="avarga@solpi.local"
REMOTE_DIR="/home/avarga/AI/solax"
WEB_DIR="/var/www/html/solax"
VENV="$REMOTE_DIR/venv"

DEPLOY_FILES=(
    solax_controller.py
    solax_poller.py
    import_prices.py
    download_pnd.py
    config.env.example
    requirements.txt
)

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

echo "Restarting services..."
ssh "$REMOTE" "sudo systemctl restart solax-controller solax-poller"

echo "Deploy complete."
echo ""
echo "Quick start on $REMOTE:"
echo "  sudo mkdir -p /etc/solax /var/lib/solax"
echo "  sudo cp $REMOTE_DIR/config.env.example /etc/solax/config.env"
echo "  # Edit /etc/solax/config.env with your inverter IP and SQLite path"
echo "  # Create /etc/solax/db.php with: <?php \\$sqlite_path = '/var/lib/solax/solax.db';"
