# MicDictado

Dictado por voz a texto, **100% local y offline**, para Windows. Hotkey global, transcribe con Whisper (faster-whisper), y pega el resultado donde esté el cursor — funciona en cualquier app (editores, terminales, navegadores, Electron).

> Para contexto del roadmap estratégico, ver [`plan_escalamiento.md`](./plan_escalamiento.md). Para análisis de migración a Mac, ver [`gap_analysis_mac.md`](./gap_analysis_mac.md).

---

## Hotkeys

| Combinación | Acción |
|---|---|
| `Ctrl+Shift+F11` | Toggle grabación (primer disparo graba, segundo transcribe y auto-pega) |
| `Ctrl+Shift+Space` | Re-pegar el último texto transcripto donde esté el cursor (útil si perdiste el foco) |
| `Ctrl+Shift+F9` | Abrir ventana de Configuración |
| `Ctrl+Shift+F12` | Mute/unmute del micrófono (proceso aparte: `mic_tray.py`) |

---

## Arranque rápido

```bash
pip install -r requirements.txt
python mic_dictado.py
```

La primera vez descarga el modelo Whisper `small` (~470 MB) a `%LOCALAPPDATA%\MicDictado\models\`. Después arranca en frío en ~3 segundos.

**Requisitos**: Python 3.10+ (probado en 3.12 y 3.14), Windows 10/11. GPU NVIDIA opcional pero recomendada — si está, se activa automáticamente con fallback transparente a CPU si falla.

---

## Mapa de archivos

| Archivo | Qué hace |
|---|---|
| **`mic_dictado.py`** | Entry point principal. Loop de grabación, transcripción Whisper, pegado Unicode, overlay, hotkeys |
| **`mic_tray.py`** | App **separada e independiente**: widget circular en la barra de tareas para mute/unmute del mic con `Ctrl+Shift+F12`. Se puede usar sin `mic_dictado.py` |
| **`tray.py`** | Icono del tray de MicDictado (menú Toggle/Config/Salir) + ventana de Configuración con sliders y toggles |
| **`settings.py`** | Singleton de configuración persistente. JSON en `%APPDATA%\MicDictado\settings.json`, tolerante a claves nuevas/viejas |
| **`audio_ducker.py`** | Baja el volumen de Spotify/YouTube/etc. al 25% mientras grabás; restaura apenas soltás el hotkey |
| **`cuda_dlls.py`** | Registra las DLLs de CUDA (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`, etc.) antes de importar `faster_whisper`. Sin esto, en Windows sin CUDA Toolkit del sistema, ctranslate2 crashea al primer encode |
| **`MicDictado.spec` / `MicToggle.spec`** | Specs de PyInstaller para empaquetar cada app a `.exe` standalone |
| **`bench_whisper.py` / `bench_llm.py`** | Scripts de benchmark (latencia, RAM, calidad) usados en las Fases 0 y 1 del roadmap |

---

## Dónde se guardan los datos

Todos los datos del usuario viven en `%LOCALAPPDATA%\MicDictado\` y `%APPDATA%\MicDictado\`:

| Path | Contenido |
|---|---|
| `%APPDATA%\MicDictado\settings.json` | Configuración persistente (modelo, hotkeys, ducking, etc.) |
| `%LOCALAPPDATA%\MicDictado\models\` | Modelos Whisper descargados (~470 MB para `small`) |
| `%LOCALAPPDATA%\MicDictado\transcripts.jsonl` | Historial local de transcripciones (texto plano, sin audio). Rotación automática a 500 entradas |
| `%LOCALAPPDATA%\MicDictado\llm\` | Modelo LLM opcional (Llama 3.2 3B Q4_K_M, ~1.9 GB). **No se carga al arranque** — infraestructura preparada para features futuras (ver `plan_escalamiento.md`) |

---

## Stack técnico

- **Transcripción**: [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) (Whisper de OpenAI con CTranslate2). Default: modelo `small`, español, `beam_size=1`, VAD activo, int8 en CPU o float16 en GPU.
- **Captura de audio**: `sounddevice` + `numpy` (PortAudio backend, 16 kHz mono int16).
- **Pegado universal**: `SendInput` con `KEYEVENTF_UNICODE` carácter por carácter. **No usa clipboard** — funciona en terminales (cmd, PowerShell, Windows Terminal), terminales de Cursor/VSCode (Electron), navegadores, todo. Es la mayor diferenciación técnica del proyecto vs. competidores que usan `Ctrl+V`.
- **Control de mic** (mute/unmute, ducking): `pycaw` + `comtypes` (Windows Core Audio APIs vía COM).
- **Hotkeys globales**: `ctypes.windll.user32.RegisterHotKey` (no `keyboard` ni `pynput` — Windows nativo, sin dependencias).
- **UI**: Tkinter + Pillow. Overlay con `transparentcolor` para esquinas redondeadas. Tray con `pystray`.
- **LLM local** (opcional, infraestructura sin uso activo): `llama-cpp-python` + Llama 3.2 3B Q4_K_M GGUF.

---

## Detalles de diseño no obvios

- **Single-instance via lockfile + taskkill**: si abrís una segunda instancia, la primera se mata sola y la nueva queda corriendo. Útil en desarrollo (sin pasar por Task Manager) y para zombies. Cada app (`mic_dictado.py` y `mic_tray.py`) tiene su propio lockfile.
- **Release de modificadores virtual antes de tipear**: cuando se dispara desde un hotkey, `Ctrl+Shift` siguen físicamente pulsados. Sin soltarlos virtualmente con `SendInput KEYUP`, las primeras letras del texto se interpretan como atajos (`Ctrl+B`, `Ctrl+S`, etc.) en la app destino.
- **Mute/ducking se restauran al soltar el hotkey**, no al terminar la transcripción. El usuario espera silencio físico inmediato cuando suelta `F11`.
- **Pre-warmup del modelo**: al cargar Whisper, se transcribe 0.5s de silencio dummy para compilar/cachear los kernels CUDA. Sin esto, la primera dictada real paga ~0.3–1s extra de JIT.
- **Detector de loops repetitivos**: Whisper bajo presión de memoria o con audio degradado a veces devuelve "X X X X X" indefinidamente. Si se detecta, se reprocesa con `beam_size=5`, `vad_filter=False`, `condition_on_previous_text=False` (más robusto). Si el loop persiste, se descarta y se muestra "Audio no entendido" 2s.

---

## Empaquetado a `.exe`

```bash
pyinstaller MicDictado.spec
pyinstaller MicToggle.spec
```

Los binarios quedan en `dist/`. El `.spec` ya incluye DLLs CUDA, ctranslate2, tokenizers, sounddevice/PortAudio y llama-cpp.
