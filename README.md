# AMP AutoPower

Aplicación de apagado y acciones de energía programadas para CachyOS/Arch Linux con KDE Plasma.

## Funciones

- Varias programaciones independientes.
- Apagar, reiniciar, suspender, hibernar, cerrar sesión, bloquear sesión y modo de prueba.
- Días de la semana configurables.
- Programaciones por hora, intervalo one-shot o exclusivamente por inactividad.
- Condiciones de inactividad, CPU y red combinables mediante lógica **AND/OR**.
- Avisos a 30, 15, 5 y 1 minuto cuando se usa una hora programada.
- Cuenta regresiva final con **Cancelar**, **Posponer 10 min** y **Posponer 30 min**.
- Cierre seguro de aplicaciones antes de apagar o reiniciar.
- Cierre limpio de Google Chrome para conservar ventanas y pestañas.
- Notificaciones y sonido.
- Display compacto negro/verde con transparencia y formato de 12/24 horas.
- Icono en la bandeja de KDE configurable.
- Atajo global mediante KGlobalAccel en Plasma Wayland.
- Comando previo opcional con timeout y política de fallo, y comando opcional al cancelar.
- Inicio automático con `systemd --user`.
- Registro de actividad y respaldos antes de actualizar.
- Búsqueda de actualizaciones cada 48 horas.
- Actualización manual o por Internet con verificación SHA-256.
- El actualizador espera a que pulses **OK** antes de reiniciar AMP AutoPower.

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

## CLI

```bash
amp-autopower --show
amp-autopower --hide
amp-autopower --toggle
amp-autopower --status
amp-autopower --list-schedules
amp-autopower --enable ID_O_NOMBRE
amp-autopower --disable ID_O_NOMBRE
```

Las órdenes de interfaz y administración se envían a la instancia en ejecución mediante IPC. Las consultas de estado y programaciones también funcionan sin conexión.

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

## AMP AutoPower 2.0.0

El motor conserva las ocurrencias pendientes y solo registra una ejecución cuando la acción termina correctamente. Las programaciones por intervalo son de una sola ejecución; las condiciones de hora, inactividad, CPU y red pueden combinarse con AND u OR.

El editor separa activación, condiciones, acción y avisos en pestañas. La cuenta regresiva permite cancelar o posponer, y el display compacto puede permanecer visible con la transparencia y el formato horario elegidos.

Las configuraciones y el estado de 1.3.0 se cargan automáticamente con valores seguros para las opciones nuevas.
