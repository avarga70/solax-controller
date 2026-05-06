#!/bin/bash
# Deploy solar_controller.py (and optionally other files) to both nodes
# Usage: ./deploy.sh [--restart]

set -euo pipefail

NODES=(node1 node2)
REMOTE_DIR="/home/avarga/AI/solar"
WEB_DIR="/data/www/webdav/solar"
DEPLOY_FILES=(
    solar_controller.py
    check_inverter.py
    download_pnd.py
    run_pnd.sh
    config.env.example
    planned_outages.json
    requirements.txt
)

RESTART=0
for arg in "$@"; do
    [[ "$arg" == "--restart" ]] && RESTART=1
done

for node in "${NODES[@]}"; do
    echo "=== Deploying to $node ==="
    ssh "$node" "mkdir -p $REMOTE_DIR"
    rsync -av --files-from=<(printf '%s\n' "${DEPLOY_FILES[@]}") \
        . "$node:$REMOTE_DIR/"

    echo "  Deploying web UI to $node:$WEB_DIR ..."
    ssh "$node" "mkdir -p $WEB_DIR"
    rsync -av solar_web/ "$node:$WEB_DIR/"

    if [[ $RESTART -eq 1 ]]; then
        echo "  Restarting solar-controller on $node..."
        ssh "$node" "sudo systemctl restart solar-controller" && echo "  Restarted."
    fi
done

echo ""
echo "Done. To also restart the service: ./deploy.sh --restart"
echo ""
echo "First-time web UI setup (run once per node):"
echo "  sudo mkdir -p /etc/solar"
echo "  sudo cp $WEB_DIR/db.php.example /etc/solar/db.php"
echo "  sudo nano /etc/solar/db.php          # fill in credentials"
echo "  sudo htpasswd -c /etc/solar/.htpasswd solar   # create web UI user"
echo "  sudo chmod 640 /etc/solar/db.php /etc/solar/.htpasswd"
