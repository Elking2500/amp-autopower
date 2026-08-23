#!/usr/bin/env bash
set -Eeuo pipefail

APP_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="install"
[[ "${1:-}" == "--update" ]] && MODE="update"

VERSION="$(tr -d '[:space:]' < "$APP_SRC/VERSION")"
APP_SHARE="$HOME/.local/share/amp-autopower"
APP_BIN="$HOME/.local/bin/amp-autopower"
DESKTOP_FILE="$HOME/.local/share/applications/amp-autopower.desktop"
SERVICE_FILE="$HOME/.config/systemd/user/amp-autopower.service"
STATE_DIR="$HOME/.local/state/amp-autopower"
BACKUP_ROOT="$STATE_DIR/backups"
OLD_SERVICE="$HOME/.config/systemd/user/ampwinoff.service"
MIGRATION_BACKUP="$HOME/.config/amp-autopower/migration-backup-$(date +%Y%m%d-%H%M%S)"
UPDATE_LOG="$STATE_DIR/update.log"

mkdir -p "$STATE_DIR" "$BACKUP_ROOT"
exec >> >(tee -a "$UPDATE_LOG") 2>&1

echo "[$(date '+%F %T')] Instalando AMP AutoPower v$VERSION (modo: $MODE)"

if ! python3 -c 'import PySide6' >/dev/null 2>&1; then
  echo "Falta PySide6. En CachyOS/Arch instala primero:"
  echo "  sudo pacman -S --needed pyside6 python-evdev libnotify"
  exit 2
fi

if ! python3 -c 'import evdev' >/dev/null 2>&1; then
  echo "Falta python-evdev. Instala: sudo pacman -S --needed python-evdev"
  exit 2
fi

mkdir -p \
  "$HOME/.config/systemd/user" \
  "$HOME/.local/share/applications" \
  "$HOME/.local/bin" \
  "$HOME/.config/amp-autopower" \
  "$APP_SHARE"

# Respaldo de la versión instalada. No toca config.json ni state.json.
if [[ -f "$APP_SHARE/amp_autopower.py" ]]; then
  CURRENT_VERSION="$(cat "$APP_SHARE/VERSION" 2>/dev/null || echo desconocida)"
  BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)-v${CURRENT_VERSION}"
  mkdir -p "$BACKUP_DIR"
  cp -a "$APP_SHARE/." "$BACKUP_DIR/"
  echo "Respaldo de aplicación: $BACKUP_DIR"
fi

# Migración segura del viejo apagador.
if [[ -f "$OLD_SERVICE" ]]; then
  mkdir -p "$MIGRATION_BACKUP"
  cp -a "$OLD_SERVICE" "$MIGRATION_BACKUP/"
  if [[ -f "$HOME/.local/bin/ampwinoff-linux.py" ]]; then
    cp -a "$HOME/.local/bin/ampwinoff-linux.py" "$MIGRATION_BACKUP/"
  fi
  systemctl --user stop ampwinoff.service 2>/dev/null || true
  systemctl --user disable ampwinoff.service 2>/dev/null || true
  echo "Servicio anterior respaldado en: $MIGRATION_BACKUP"
fi

install -Dm755 "$APP_SRC/amp_autopower.py" "$APP_SHARE/amp_autopower.py"
install -Dm644 "$APP_SRC/VERSION" "$APP_SHARE/VERSION"
install -Dm644 "$APP_SRC/README.md" "$APP_SHARE/README.md"

cat > "$APP_BIN" <<EOF
#!/usr/bin/env bash
exec python3 "$APP_SHARE/amp_autopower.py" "\$@"
EOF
chmod 755 "$APP_BIN"

# Escritorio con ruta absoluta para que KDE no dependa del PATH.
sed "s|@APP_EXEC@|$APP_BIN|g" "$APP_SRC/amp-autopower.desktop" > "$DESKTOP_FILE"
chmod 644 "$DESKTOP_FILE"
install -Dm644 "$APP_SRC/amp-autopower.service" "$SERVICE_FILE"

systemctl --user daemon-reload
systemctl --user enable amp-autopower.service >/dev/null

# En la primera migración, convertir el antiguo /usr/bin/amp-autopower en un
# lanzador hacia la instalación de usuario. Esto requiere sudo una sola vez.
if [[ "$MODE" == "install" && -e /usr/bin/amp-autopower ]]; then
  echo
  echo "Se detectó la instalación antigua en /usr/bin/amp-autopower."
  echo "La convertiré en un pequeño lanzador a ~/.local/bin para que futuras actualizaciones no necesiten sudo."
  WRAPPER="$(mktemp)"
  cat > "$WRAPPER" <<'EOF'
#!/usr/bin/env bash
exec "$HOME/.local/bin/amp-autopower" "$@"
EOF
  chmod 755 "$WRAPPER"
  if sudo install -m755 "$WRAPPER" /usr/bin/amp-autopower; then
    echo "Lanzador legado actualizado correctamente."
  else
    echo "Aviso: no se pudo reemplazar /usr/bin/amp-autopower. El servicio y el menú de KDE sí usarán la nueva versión."
  fi
  rm -f "$WRAPPER"
fi

if [[ "$MODE" == "update" ]]; then
  # Dar tiempo al actualizador que lanzó este script para terminar su diálogo.
  ( sleep 1; systemctl --user restart amp-autopower.service ) &
  disown || true
else
  systemctl --user restart amp-autopower.service 2>/dev/null || systemctl --user start amp-autopower.service
fi

echo
echo "AMP AutoPower v$VERSION instalado correctamente."
echo "Ejecutable principal: $APP_BIN"
echo "Abrir interfaz:       $APP_BIN --show"
echo "Estado:               systemctl --user status amp-autopower.service"
echo "Registro:             ~/.local/state/amp-autopower/amp-autopower.log"
echo "Registro actualización: ~/.local/state/amp-autopower/update.log"
