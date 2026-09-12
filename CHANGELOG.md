# Changelog

## 2.0.0

- Nuevo motor de condiciones con programaciones por hora o intervalo one-shot y condiciones de inactividad, CPU y red combinables mediante lógica AND/OR.
- Ocurrencias pendientes persistentes para conservar el objetivo programado mientras se cumplen las condiciones, incluso al cruzar medianoche.
- Correcciones de Cancelar y Posponer, con opciones de 10 o 30 minutos y protección frente a cuentas regresivas duplicadas.
- Las acciones fallidas ya no consumen la ocurrencia ni se registran incorrectamente en `last_runs`; los intervalos solo se completan tras un resultado satisfactorio.
- Display compacto negro/verde inspirado en WinOFF, con transparencia, formato de 12/24 horas, posición persistente e integración con el tema de KDE.
- Editor de programaciones reorganizado por pestañas.
- Nuevas acciones para cerrar sesión y bloquear la sesión.
- Comando previo opcional con espera, timeout y política de cancelar o continuar si falla.
- Comando opcional al cancelar explícitamente una cuenta regresiva.
- Preferencias globales para display, bandeja configurable, inicio minimizado y atajo real mediante KGlobalAccel en Plasma Wayland.
- CLI administrativa con `--show`, `--hide`, `--toggle`, `--status`, `--list-schedules`, `--enable` y `--disable`.
- IPC ampliado para controlar una única instancia sin iniciar un segundo scheduler.
- Compatibilidad de configuración y estado al actualizar desde 1.3.0.

## 1.3.0

- Nuevo modo de programación exclusivamente por inactividad, sin hora fija.
- La hora programada puede activarse o desactivarse individualmente para cada programación.
- Las programaciones por inactividad se rearman únicamente después de nueva actividad.
- Posponer una acción por inactividad sigue funcionando; si el usuario vuelve a usar la PC, comienza un ciclo nuevo.
- Nueva opción para cerrar correctamente las aplicaciones antes de apagar o reiniciar.
- El cierre seguro utiliza `org.kde.Shutdown.logoutAndShutdown` y `logoutAndReboot` de Plasma.
- Google Chrome recibe una solicitud de salida limpia antes del cierre de Plasma para conservar correctamente ventanas y pestañas.
- AMP AutoPower solo envía la señal de cierre a los procesos principales de Google Chrome, nunca a renderer, GPU, utility o zygote.
- Si Chrome no termina correctamente en 15 segundos, AMP AutoPower cancela el apagado/reinicio en lugar de forzar su cierre.
- El servicio espera a que el socket de Wayland esté disponible durante el inicio de sesión, evitando abortos/core dumps de Qt al arrancar demasiado pronto.
- El actualizador instala primero la nueva versión, muestra la confirmación de instalación y reinicia AMP AutoPower únicamente después de que el usuario pulse OK.

## 1.2.2

- Corregida la búsqueda de actualizaciones que podía tardar ~60 segundos con urllib e IPv6.
- El canal de actualizaciones usa IPv4 sin desactivar IPv6 en el sistema.
- La descarga de actualizaciones utiliza la misma ruta IPv4 rápida.
- El reinicio posterior a una actualización se programa fuera del cgroup de AMP AutoPower.
- Añadido TimeoutStopSec=10s para evitar esperas excesivas al detener el servicio.

## 1.2.1

- Overlay de emergencia probado sobre juegos Linux y Wine/Proton a pantalla completa.
- Detección global de actividad mediante evdev.
- Compatibilidad con mouse, teclado, touchpad, joystick y mandos USB/Bluetooth/wireless.
- Corregida compatibilidad con python-evdev 1.9.x usando os.set_blocking().
- Excluidos sensores de movimiento, giroscopios y acelerómetros para evitar falsa actividad.
- Opción para exigir inactividad antes de apagar, reiniciar, suspender o hibernar.

## 1.2.0

- Overlay fullscreen.
- Inactividad global evdev.
- Mandos USB/Bluetooth/wireless.
- Requisito de inactividad por programación.

## 1.1.1 - 2026-08-23

- Conecta AMP AutoPower al canal oficial `Elking2500/amp-autopower`.
- Configura automáticamente el `manifest.json` remoto.
- Migra instalaciones 1.1.0 cuyo canal de actualización estaba vacío.
- Mantiene la comprobación automática cada 48 horas y la verificación SHA-256.

## 1.1.0 - 2026-08-23

- Añade pestaña de actualizaciones.
- Añade comprobación automática cada 48 horas.
- Añade actualización desde paquete local o manifiesto remoto.
- Añade respaldos antes de actualizar.
