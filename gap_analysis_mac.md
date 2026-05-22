# Gap Analysis — Migración de MicDictado a macOS

> Última actualización: 2026-05-22
> Estado del proyecto al momento del análisis: app funcional en Windows, stack Python + faster-whisper, hotkeys globales, pegado universal Unicode, tray + overlay, LLM local opcional (Llama 3.2 3B Q4).

## Resumen ejecutivo

MicDictado está hecho en Python, así que el **80 % del código de transcripción y lógica de negocio es portable tal cual**. El otro 20 % toca APIs nativas de Windows (audio control, hotkeys globales, pegado Unicode, tray) y hay que reescribirlo con equivalentes macOS. **Esfuerzo estimado: 2–4 días de trabajo enfocado** para tener un MVP funcional en Mac, más 1–2 días si se quiere distribución firmada/notarizada.

La buena noticia: en **Apple Silicon (M1/M2/M3/M4)** el stack de inferencia local corre **mejor que en Windows** porque `llama-cpp-python` tiene soporte nativo para Metal (GPU integrada) sin DLLs CUDA, y `whisper.cpp` también acelera por Metal.

---

## Requisitos mínimos sugeridos para la Mac

| Item | Mínimo | Recomendado | Notas |
|---|---|---|---|
| **macOS** | 12 Monterey | 14 Sonoma o superior | macOS 13+ trae mejoras de Metal y de wheels Python que importan |
| **Chip** | Intel x86_64 | **Apple Silicon (M1 en adelante)** | Intel funciona pero Whisper queda solo en CPU. M-series acelera Whisper y LLM por Metal/Neural Engine |
| **RAM** | 8 GB | **16 GB** | Con 8 GB solo se puede correr Whisper `small` cómodo. Para usar el LLM 3B en paralelo, 16 GB |
| **Disco** | 2 GB libres | 5 GB libres | Whisper small ~470 MB + LLM Q4 ~1.9 GB + Python + binarios |
| **Python** | 3.10 | 3.12 | Evitar 3.14 hasta que `llama-cpp-python` publique wheels Metal estables |

**Recomendación corta para tu amigo**: si tiene una Mac M1 o más nueva con 16 GB de RAM y macOS 13+, va a andar perfecto. Si tiene un MacBook Intel viejo con 8 GB, va a poder usarlo pero la latencia va a ser visiblemente peor.

---

## Lo que se reutiliza tal cual (sin cambios)

| Componente | Archivo | Por qué portea sin cambios |
|---|---|---|
| Lógica de transcripción | `mic_dictado.py` (núcleo) | `faster-whisper` corre en macOS (CPU). El flujo grabar → buffer → transcribir → pegar es agnóstico al OS |
| Detección de loops repetitivos | `_has_repetitive_loop` | Algoritmo puro de strings |
| Persistencia de settings | `settings.py` | JSON puro, sin APIs del OS |
| Historial JSONL | `_log_transcript` | I/O estándar |
| Stack LLM (opcional) | `_load_llm`, `_correct_with_glossary` | `llama-cpp-python` soporta Metal nativamente en Mac, **incluso mejor que el setup CUDA de Windows** |
| Overlay visual | `DictadoOverlay` | Mayoría es Pillow + Tkinter; ajustes menores (ver más abajo) |

---

## Lo que hay que reemplazar (Windows-specific)

| Componente Windows | Función | Reemplazo macOS | Esfuerzo |
|---|---|---|---|
| **`pycaw` + COM (mute mic)** | Leer/escribir mute del micrófono por defecto | `pyobjc` + `AVFoundation` (o `osascript` como fallback). Alternativa: omitir mute automático y dejar al usuario manejarlo manual | Medio (4–6 h) |
| **`pycaw` (audio ducking)** | Bajar volumen de Spotify/etc. mientras grabás | macOS no expone control granular por proceso como Windows. Opciones: (a) `osascript` para apps específicas (Spotify, Music), (b) bajar volumen global con `AppleScript`, (c) **descartar la feature de ducking en Mac v1** | Medio-alto si se quiere paridad; cero si se omite |
| **`ctypes.windll.user32.RegisterHotKey`** | Hotkeys globales (F11, Shift+Space, F9) | `pynput.keyboard.GlobalHotKeys` o `pyobjc` + `Quartz` Event Tap. **Requiere permiso de Accessibility** otorgado por el usuario | Bajo-medio (3–4 h) |
| **`SendInput` Unicode (pegado universal)** | Tipear texto carácter por carácter en cualquier app | `Quartz.CGEventCreateKeyboardEvent` + `CGEventKeyboardSetUnicodeString`. **Requiere permiso de Accessibility**. Funciona en Terminal, VSCode, Chrome, etc. — equivalente directo de SendInput Unicode | Medio (4–6 h) |
| **`_release_modifiers()` para soltar Ctrl+Shift** | Evitar que las primeras letras se interpreten como atajos | En macOS los modifiers son distintos (Cmd/Option). Misma técnica con CGEventCreateKeyboardEvent enviando key-up de modifiers | Bajo (1 h) |
| **Tray icon (`pystray._win32`)** | Icono en la barra de tareas | `pystray` tiene backend macOS (`pystray._darwin`) pero limitado. Mejor: **`rumps`** (librería específica de menubar Mac, mucho más limpia) | Bajo (2–3 h) |
| **`-transparentcolor` de Tkinter** | Esquinas redondeadas del overlay (color clave magenta) | **macOS NO soporta `-transparentcolor`**. Hay que usar `-alpha` (transparencia global) o reescribir el overlay con `tkinter.Canvas` + ventana sin borde + alpha, aceptando esquinas no perfectamente redondeadas. Alternativa más prolija: rehacer el overlay con `pyobjc` + `NSPanel` (más laburo, mejor look) | Medio (3–5 h) si se acepta degradación visual; alto si se quiere paridad estética |
| **`-toolwindow` de Tkinter** | Que no aparezca en alt-tab | macOS lo ignora silenciosamente. Hay que usar `pyobjc` o aceptar que aparezca en Cmd+Tab | Bajo (omitir o 2 h con pyobjc) |
| **CUDA DLLs (`cuda_dlls.py`, `nvidia-*` packages)** | GPU offload de Whisper en NVIDIA | **Borrar todo eso en Mac**. En Apple Silicon, `faster-whisper` no usa Metal directamente (limitación de CTranslate2). Opciones: (a) correr en CPU (M1+ es lo bastante rápido para `small`), (b) **migrar a `mlx-whisper`** (framework de Apple, GPU nativa, muy rápido), (c) usar `whisper.cpp` con Metal vía `pywhispercpp` | Cero (CPU) a medio (3–4 h si se integra MLX) |
| **`LOCALAPPDATA` env var** | Ubicar carpeta de datos | Reemplazar por `~/Library/Application Support/MicDictado/` (convención macOS) | Trivial (15 min) |
| **`taskkill /F /PID` + `creationflags=CREATE_NO_WINDOW`** | Single-instance: matar instancia previa | `os.kill(pid, signal.SIGTERM)` puro Python | Trivial (30 min) |
| **`C:/Windows/Fonts/segoeuib.ttf`** | Carga de fuente del overlay | `/System/Library/Fonts/SFNS.ttf` (San Francisco) o `/Library/Fonts/Helvetica.ttc`. Fallback a `ImageFont.load_default()` ya está | Trivial (15 min) |
| **PyInstaller `.spec` Windows** | Empaquetado a `.exe` | Nuevo `.spec` para `.app` bundle macOS. Necesita `Info.plist` con `NSMicrophoneUsageDescription` declarando que se usa el mic | Medio (3–4 h) |

---

## Permisos macOS que el usuario va a tener que otorgar

macOS es más estricto que Windows con privacidad. La primera vez que se corre la app, **el usuario va a tener que ir a Configuración del Sistema y habilitar manualmente**:

1. **Micrófono** (Privacidad y Seguridad → Micrófono): para grabar audio. macOS muestra un prompt automático la primera vez.
2. **Accesibilidad** (Privacidad y Seguridad → Accesibilidad): para registrar hotkeys globales y simular tipeo. **No hay prompt automático prolijo** — hay que documentárselo al usuario, sino la app va a parecer rota (los hotkeys no responden y el pegado no aparece).
3. **Monitoreo de entrada** (Privacidad y Seguridad → Monitoreo de entrada): a veces requerido además de Accesibilidad para hotkeys globales según la versión de macOS.

Esto es **lo más friccionado del onboarding en Mac**. Conviene tener un primer-arranque guiado que abra estos paneles automáticamente (`open "x-apple.systempreferences:com.apple.preference.security"`).

---

## Distribución (si se quiere mandar el `.app` listo)

Tres niveles de fricción al instalar en otra Mac:

| Modo | Qué ve el usuario | Esfuerzo |
|---|---|---|
| **Sin firmar** (lo que sale de PyInstaller crudo) | "No se puede abrir porque Apple no puede comprobar que esté libre de malware" → hay que click derecho → Abrir → confirmar. Funciona, pero asusta | Cero |
| **Firmado con Developer ID** (Apple Developer Program, USD 99/año) | Abre sin alertas en la mayoría de Macs, pero Gatekeeper puede pedir confirmación la primera vez | 1–2 h (signing) + USD 99/año |
| **Firmado + notarizado** (estándar para distribución pública) | Abre sin ninguna alerta | 2–4 h adicionales (notarytool) |

Para uso entre amigos, **sin firmar alcanza**: explicale el click derecho → Abrir la primera vez y listo. Para distribuir más en serio, firmado + notarizado.

---

## Estimación total de esfuerzo

| Fase | Alcance | Tiempo |
|---|---|---|
| **Fase 1 — MVP funcional** | Hotkeys + grabación + transcripción + pegado Unicode en Mac. Sin ducking, sin tray pulido, overlay simplificado. App corriendo desde `python mic_dictado_mac.py` | **2 días** |
| **Fase 2 — Pulido** | Tray con `rumps`, overlay con esquinas redondeadas (alpha-based o NSPanel), aceleración Metal vía MLX o whisper.cpp, ducking opcional con `osascript` | **2 días** |
| **Fase 3 — Empaquetado** | `.app` bundle con PyInstaller, `Info.plist` con permisos, primer-arranque guiado para Accessibility | **1 día** |
| **Fase 4 — Distribución firmada** (opcional) | Apple Developer ID, signing, notarización | **1 día + USD 99/año** |

**Total realista**: 5–6 días para una v1 distribuible bien terminada. 2 días para una v1 "anda en mi Mac".

---

## Estrategia recomendada de implementación

**No mantener una sola base de código cross-platform.** Recomiendo:

1. Crear una rama `mac` (o un módulo `platform_mac/`) y reescribir las partes Windows-specific como capa de abstracción:
   - `platform_audio.py` — mute, ducking, level
   - `platform_hotkeys.py` — registrar/escuchar hotkeys
   - `platform_typing.py` — tipeo Unicode
   - `platform_tray.py` — icono de barra
2. El núcleo (`mic_dictado.py` adelgazado) consume esa capa, sin importar `ctypes.windll` ni `pycaw`.
3. Esto deja el camino abierto para mantener Windows y Mac en un mismo repo sin `if sys.platform` regados.

Si el friend solo quiere usarlo y no mantenerlo, una versión Mac-only standalone es más rápida (no hay que abstraer nada, se reemplaza directo).

---

## Lo que NO se va a poder portar (o no vale la pena)

- **`mic_tray.py`** (el widget circular en la barra de tareas con halo de VU meter angular): macOS no tiene equivalente a un widget arbitrario flotante en la barra de menús — solo iconos de menubar. La feature equivalente es un icono cambiante en la menubar; el VU angular se pierde. No es bloqueante, solo es eye-candy específico de Windows.
- **Cualquier feature futura de "comandos por voz a Slack/Gmail vía API" (Fase 2 del roadmap)**: 100 % portable. Es Python puro, mismo stack.

---

## Próximos pasos sugeridos para tu amigo

1. **Verificar specs de su Mac**: modelo, chip, RAM, versión de macOS. Idealmente M1+, 16 GB, macOS 13+.
2. **Decidir scope**: ¿quiere solo dictado básico (MVP de 2 días), o paridad completa con la versión Windows?
3. **Permisos**: tener claro que va a tener que autorizar Accesibilidad + Micrófono. Sin eso no hay forma de hacer funcionar hotkeys globales ni pegado universal en macOS.
4. **Si va a haber distribución**: decidir si vale el Developer ID de Apple (USD 99/año) o si alcanza con el bypass manual.
