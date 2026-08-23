#!/usr/bin/env bash
set -Eeuo pipefail
systemctl --user disable --now amp-autopower.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/amp-autopower.service"
rm -f "$HOME/.local/share/applications/amp-autopower.desktop"
rm -f "$HOME/.local/bin/amp-autopower"
rm -rf "$HOME/.local/share/amp-autopower"
systemctl --user daemon-reload
echo "AMP AutoPower desinstalado. La configuración y respaldos se conservan en ~/.config/amp-autopower y ~/.local/state/amp-autopower/."
echo "Si todavía existe /usr/bin/amp-autopower de una instalación antigua, puedes eliminarlo manualmente con: sudo rm -f /usr/bin/amp-autopower"
