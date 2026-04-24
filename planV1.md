# Plan v1: MicDictado — App de dictado (voice-to-text)

Segunda app del proyecto, hermana de MicToggle. Permite dictar por voz y pegar el texto transcripto donde esté el cursor.

## Resumen

- **Activación**: hotkey global `Ctrl+Shift+F11` (mapeable al botón lateral del mouse desde el software del mouse).
- **Modo**: toggle. Primer disparo empieza a grabar, segundo termina → transcribe → pega.
- **Relación con MicToggle**: si el mic está muteado al disparar, se desmutea automáticamente, se dicta, y al terminar se restaura el estado previo. Reutiliza `pycaw + IAudioEndpointVolume`.
- **STT**: `faster-whisper` modelo `small` int8, CPU, local, offline, gratis.

## Decisiones técnicas

| Tema | Decisión |
|------|----------|
| Arquitectura | App separada: `mic_dictado.py` → `MicDictado.exe` |
| Hotkey | Ctrl+Shift+F11 |
| Modo | Toggle |
| Compartir `get_mic_volume()` | Copiar las 6 líneas (evitar side-effects del import de mic_tray) |
| Captura audio | `sounddevice` @ 16 kHz mono int16 |
| Pegado | `pyperclip` + SendInput Ctrl+V, preservando clipboard previo (restore a 300ms) |
| Modelo | `small` int8, descarga lazy a `%LOCALAPPDATA%\MicDictado\models\` |
| Feedback visual | Overlay Tkinter: idle / cargando / grabando / transcribiendo |
| Estado mic post-dictado | Restaurar al estado previo |

## Dependencias

```
pip install -r requirements.txt
```

Contenido:
- `pycaw` — control de audio endpoint (mute/unmute)
- `comtypes` — COM bindings para pycaw
- `sounddevice` — captura de audio PortAudio
- `numpy` — buffer de audio
- `faster-whisper` — STT local (trae `ctranslate2`, `tokenizers`)
- `pyperclip` — manejo de clipboard

## Fases de entrega (commit por cada una)

### Fase 0 — Setup de repo
- `planV1.md` y `requirements.txt` creados.
- `pip install -r requirements.txt` ejecutado.
- Verificación: `python -c "import sounddevice, faster_whisper, pyperclip; print('OK')"`.

### Fase 1 — Esqueleto + hotkey + overlay
- `mic_dictado.py` con mutex, hotkey F11, overlay Tk con estados.
- Toggle alterna overlay entre `idle` y `grabando` (sin grabar audio todavía).

### Fase 2 — Captura de audio + mute auto
- `sounddevice.InputStream` 16 kHz mono, buffer en memoria.
- Retry COM helper con patrón de `mic_tray.py:137-141`.
- En cada toggle guarda/restaura estado mute.
- WAV temporal para inspección.

### Fase 3 — Transcripción faster-whisper
- `WhisperModel("small", device="cpu", compute_type="int8")` cargado en hilo al inicio.
- `model.transcribe(audio, language="es", beam_size=5)`.
- Resultado por `print` en esta fase.

### Fase 4 — Pegado con preservación de clipboard
- `paste_text(texto)`: backup clipboard → copy → SendInput Ctrl+V → restore a 300ms.
- Sustituye el `print` de fase 3.
- Test end-to-end en Notepad, VSCode, Chrome, Windows Terminal.

### Fase 5 — PyInstaller
- `MicDictado.spec` con `hiddenimports=['ctranslate2','tokenizers']`, `collect_all` para `faster_whisper` y `ctranslate2`, `collect_binaries` para `sounddevice`.
- `pyinstaller MicDictado.spec`.

### Fase 6 — Validación y pulido
- Post-sleep: suspender/reanudar → dictar sin crash.
- Buffer vacío, modelo aún cargando, etc.
- README de uso y mapeo del botón del mouse.

## Verificación end-to-end

1. Primer run descarga modelo con progreso en overlay.
2. Dictado básico: Notepad + F11 + hablar + F11 → texto pegado.
3. Dictado con mic muteado (feature clave): mutear F12, dictar F11 → texto OK + mic restaurado a muteado.
4. Acentos/ñ correctos en Chrome, VSCode, Windows Terminal.
5. Post-sleep: suspender/reanudar → dictar sin crash.
6. Clipboard preservado: Ctrl+C algo, dictar, Ctrl+V → sale el texto original.
7. Coexistencia MicToggle (F12) + MicDictado (F11) sin interferencias.

## Referencias a `mic_tray.py`

| Qué | Dónde | Para qué |
|-----|-------|----------|
| `get_mic_volume()` | líneas 29-34 | Copiar |
| Mutex pattern | líneas 21-26 | Replicar con `MicDictadoMutex` |
| RegisterHotKey + WM_HOTKEY | líneas 103-109 | Replicar para F11 |
| Retry COM tras fallo | líneas 132-142 | Envolver Get/SetMute |
| Overlay Tk flags | líneas 67-71 | Mismo estilo |
| PyInstaller .spec | `MicToggle.spec` | Clonar |

## Archivos nuevos

- `planV1.md` (Fase 0)
- `requirements.txt` (Fase 0)
- `mic_dictado.py` (Fases 1-4, 6)
- `MicDictado.spec` (Fase 5)

**`mic_tray.py` no se modifica en ninguna fase.**
