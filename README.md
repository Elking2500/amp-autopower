# AMP AutoPower

Aplicación de apagado y acciones de energía programadas para CachyOS/Arch Linux con KDE Plasma.

## Funciones

- Varias programaciones independientes.
- Apagar, reiniciar, suspender, hibernar y modo de prueba.
- Días de la semana configurables.
- Hora programada opcional para cada programación.
- Programaciones exclusivamente por inactividad, sin una hora fija.
- Tiempo mínimo de inactividad configurable por programación.
- Avisos a 30, 15, 5 y 1 minuto cuando se usa una hora programada.
- Cuenta regresiva final con **Cancelar**, **Posponer 10 min** y **Posponer 30 min**.
- Cierre seguro de aplicaciones antes de apagar o reiniciar.
- Cierre limpio de Google Chrome para conservar ventanas y pestañas.
- Notificaciones y sonido.
- Icono en la bandeja de KDE.
- Inicio automático con `systemd --user`.
- Registro de actividad y respaldos antes de actualizar.
- Búsqueda de actualizaciones cada 48 horas.
- Actualización manual o por Internet con verificación SHA-256.
- El actualizador espera a que pulses **OK** antes de reiniciar AMP AutoPower.

## Novedades de 1.1.1

- Canal oficial de actualizaciones conectado a este repositorio de GitHub.
- El `manifest.json` oficial se configura automáticamente.
- Migración de configuraciones 1.1.0 que tenían la URL del canal vacía.
- Se mantienen intactos los horarios y preferencias existentes.

## Instalación en CachyOS / Arch

```bash
sudo pacman -S --needed pyside6 libnotify python-evdev
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

Cuando una actualización termina correctamente, AMP AutoPower muestra la confirmación primero y reinicia la aplicación únicamente después de que pulses **OK**.

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

## v1.3.0 — Inactividad sin hora, cierre seguro y actualizador mejorado

- Cada programación puede activar o desactivar **Usar hora programada**.
- Si la hora está desactivada, la programación puede ejecutarse exclusivamente después del tiempo mínimo de inactividad.
- Las programaciones por inactividad se rearman después de nueva actividad para evitar ejecuciones repetidas durante el mismo periodo inactivo.
- **Cancelar** y **Posponer** siguen disponibles durante la cuenta regresiva final.
- En apagado y reinicio puede activarse **Cerrar aplicaciones correctamente antes de apagar/reiniciar**.
- Plasma realiza el cierre seguro mediante `logoutAndShutdown` o `logoutAndReboot`.
- Google Chrome recibe primero una solicitud de salida limpia mediante `SIGHUP`, únicamente en sus procesos principales.
- No se envía la señal directamente a procesos renderer, GPU, utility o zygote de Chrome.
- AMP AutoPower espera hasta 15 segundos a que Chrome termine correctamente.
- Si Chrome no termina dentro de ese tiempo, el apagado o reinicio se cancela en lugar de forzar el cierre.
- El estado de ventanas y pestañas de Chrome puede restaurarse normalmente en el siguiente inicio.
- El actualizador instala primero la nueva versión, muestra el resultado y reinicia AMP AutoPower únicamente después de pulsar **OK**.
- El servicio espera a que el socket de Wayland exista antes de iniciar Qt, evitando fallos de arranque cuando la sesión gráfica todavía no está preparada.

## v1.2.2 — Juegos fullscreen e inactividad global

- Overlay de emergencia sobre juegos y pantalla completa.
- Avisos previos keep-above.
- Monitor global evdev para mouse, teclado, touch y mandos USB/Bluetooth/wireless.
- Cada programación puede exigir minutos mínimos de inactividad.
- No se registran teclas, botones ni coordenadas; solo tiempo de última actividad.
