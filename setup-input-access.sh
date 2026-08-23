#!/usr/bin/env bash
set -Eeuo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/70-amp-autopower-input.rules"
DST="/etc/udev/rules.d/70-amp-autopower-input.rules"
echo "Instalando acceso uaccess para dispositivos de entrada de la sesión activa..."
sudo install -Dm644 "$SRC" "$DST"
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input --action=change || true
echo "Listo: $DST"
echo "Desconecta/reconecta un mando si no aparece de inmediato y reinicia AMP AutoPower."
