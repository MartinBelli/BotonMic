# Changelog

Versionado de MicDictado. Cada entrada nueva va arriba.

---

## v1.1 — 2026-04-25

Versión enfocada en UX, performance y feedback visual. La app pasa de
"funcional" a "fluida en uso real".

### Agregado

- **Re-pegado del último texto**: nuevo hotkey `Ctrl+Shift+Space` que tipea el
  último texto transcripto donde esté el cursor. Útil cuando perdiste el foco
  antes de que el auto-pegado terminara. Se puede usar varias veces en distintos
  lugares (el texto queda guardado hasta que dictes uno nuevo).
- **Ducking de música**: al empezar a grabar, baja automáticamente el volumen
  de Spotify, YouTube, Chrome, etc. al 25% (configurable). Restaura el volumen
  apenas soltás el hotkey de stop, sin esperar a que termine la transcripción.
  Procesos de comunicación (Discord, Teams, Zoom, Slack) se excluyen por
  default.
- **Tray icon + ventana de Configuración**: ícono en la bandeja del sistema con
  menú (Toggle dictado / Configuración / Salir). La ventana de Configuración
  expone sliders y toggles para todo: sensibilidad del VU meter, ducking,
  modelo de transcripción, beam size, VAD, y lista de procesos excluidos del
  ducking.
- **Hotkey de emergencia para Configuración**: `Ctrl+Shift+F9` abre la ventana
  de Settings sin depender del tray icon (por si Windows lo escondió).
- **Persistencia en JSON**: la configuración se guarda en
  `%APPDATA%\MicDictado\settings.json` y se carga al iniciar.

### Cambiado

- **Overlay rediseñado**: la barrita "Grabando..." pasa de un rectángulo plano
  a una píldora redondeada (radius 14 px, alpha 0.85, efecto glass). Render
  con Pillow + supersampling 2x para antialiasing limpio. El VU meter ahora
  son 10 LEDs segmentados (verde → amarillo → rojo) en lugar de una barra
  continua. El dot de estado tiene un halo pulsante mientras grabás.
- **Velocidad de transcripción**: `beam_size` bajado de 5 a 1 (greedy decoding)
  y `vad_filter` activado por default. En audios de 15-30s la mejora es de
  3-5x. Pérdida de precisión mínima en español claro.
- **Sensibilidad del VU meter**: subida de 4.0x a 7.0x por default y ahora es
  configurable desde Settings (rango 1.0 - 15.0).
- **Modelo configurable**: se puede cambiar entre `tiny` / `base` / `small`
  (default) / `medium` desde Settings. Recarga en background sin reiniciar.

### Corregido

- **Re-pegado "cortado"**: las primeras letras del re-pegado podían perderse
  porque el evento `WM_HOTKEY` llegaba con `Ctrl+Shift` físicamente pulsados,
  haciendo que la app destino interpretara los primeros caracteres como atajos
  (Ctrl+B, Ctrl+S...). Ahora se sueltan los modificadores virtualmente con un
  `SendInput KEYUP` antes de inyectar el texto Unicode.
- **Hotkey conflict check**: si otra app tiene tomada una combinación,
  `RegisterHotKey` ahora loguea un warning en lugar de fallar en silencio.

### Hotkeys finales

| Combinación        | Acción                                                |
|--------------------|-------------------------------------------------------|
| `Ctrl+Shift+F11`   | Grabar / parar (auto-pega al terminar)                |
| `Ctrl+Shift+Space` | Re-pegar el último texto donde esté el cursor         |
| `Ctrl+Shift+F9`    | Abrir ventana de Configuración                        |

### Archivos nuevos

- `settings.py` — load/save JSON con merge sobre defaults
- `audio_ducker.py` — duck/restore con `pycaw` por sesión de audio
- `tray.py` — ícono pystray + ventana tkinter de Configuración

### Dependencias agregadas

- `pystray` — tray icon multiplataforma
- `onnxruntime` — backend del VAD filter de faster-whisper

---

## v1.0 — 2026-04-01 (commit `5cf1329`)

Primera versión funcional empaquetada.

### Funcionalidad base

- Dictado voice-to-text con `Ctrl+Shift+F11` (toggle).
- Transcripción local con `faster-whisper` (modelo `small`, español, CPU int8).
- Pegado universal vía `SendInput KEYEVENTF_UNICODE` (funciona en terminales,
  Cursor/VSCode, navegadores, sin tocar el clipboard).
- Mute/unmute automático del mic (restaura el estado previo al terminar).
- Single-instance: si abrís una segunda instancia, la primera se cierra.
- Empaquetado con PyInstaller (`MicDictado.exe`, ~264 MB).
