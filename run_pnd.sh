#!/bin/bash
set -e

python3 /home/avarga/AI/solar/download_pnd.py
/home/avarga/scripts/electricity/get_consel
