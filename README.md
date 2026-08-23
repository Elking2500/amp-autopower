# AMP AutoPower

Aplicación de apagado y acciones de energía programadas para CachyOS/Arch Linux con KDE Plasma.

## Funciones

- Varias programaciones independientes.
- Apagar, reiniciar, suspender, hibernar y modo de prueba.
- Días de la semana y hora configurables.
- Avisos a 30, 15, 5 y 1 minuto.
- Cuenta regresiva final con **Cancelar**, **Posponer 10 min** y **Posponer 30 min**.
- Notificaciones y sonido.
- Icono en la bandeja de KDE.
- Inicio automático con `systemd --user`.
- Registro de actividad y respaldos antes de actualizar.
- Búsqueda de actualizaciones cada 48 horas.
- Actualización manual o por Internet con verificación SHA-256.

## Novedades de 1.1.1

- Canal oficial de actualizaciones conectado a este repositorio de GitHub.
- El `manifest.json` oficial se configura automáticamente.
- Migración de configuraciones 1.1.0 que tenían la URL del canal vacía.
- Se mantienen intactos los horarios y preferencias existentes.

## Instalación en CachyOS / Arch

```bash
sudo pacman -S --needed pyside6 libnotify
```

Extrae el paquete y ejecuta:

```bash
cd amp-autopower
./install.sh
```

## Actualizaciones

AMP AutoPower consulta automáticamente cada 48 horas:

`https://raw.githubusercontent.com/Elking2500/amp-autopower/main/manifest.json`

También puedes abrir **Actualizaciones → Buscar actualizaciones** en la aplicación.

El paquete descargado solo se instala si su SHA-256 coincide con el publicado en el manifiesto.

## Servicio

```bash
systemctl --user status amp-autopower.service
systemctl --user restart amp-autopower.service
journalctl --user -u amp-autopower.service -f
```

## Datos locales

Configuración:

`~/.config/amp-autopower/`

Registros y respaldos:

`~/.local/state/amp-autopower/`

Instalación de usuario:

`~/.local/share/amp-autopower/`

## v1.2.2 — Juegos fullscreen e inactividad global

- Overlay de emergencia sobre juegos y pantalla completa.
- Avisos previos keep-above.
- Monitor global evdev para mouse, teclado, touch y mandos USB/Bluetooth/wireless.
- Cada programación puede exigir minutos mínimos de inactividad.
- No se registran teclas, botones ni coordenadas; solo tiempo de última actividad.
