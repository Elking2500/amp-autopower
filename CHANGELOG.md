# Changelog

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
