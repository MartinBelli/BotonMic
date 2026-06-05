"""
mic_dictado.py — Dictado voice-to-text para Windows.

Hotkey global Ctrl+Shift+F11 (toggle): primer disparo empieza a grabar,
segundo disparo termina, transcribe con faster-whisper y pega donde este
el cursor. Si el mic esta muteado lo desmutea automaticamente y restaura
el estado previo al terminar.

Requiere: pip install -r requirements.txt
"""

import atexit
import ctypes
import datetime
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from ctypes import wintypes

import numpy as np
import sounddevice as sd
from PIL import Image, ImageDraw, ImageFont, ImageTk
from comtypes import CLSCTX_ALL

# Registrar DLLs CUDA del package nvidia-* ANTES de importar faster_whisper.
# Sin esto, en Windows sin CUDA Toolkit del sistema, ctranslate2 carga el modelo
# pero falla al hacer el primer encode con "cublas64_12.dll not found".
from cuda_dlls import setup_cuda_dlls
setup_cuda_dlls()

from faster_whisper import WhisperModel
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

from audio_ducker import AudioDucker
from settings import settings

# ── Hotkeys globales ──────────────────────────────────────────
# F11:           toggle grabacion. Auto-pega cuando termina la transcripcion.
# Shift+Space:   re-pega el ULTIMO texto transcripto donde este el cursor (util si el
#                auto-pegado fue al lugar equivocado por cambio de foco).
# F9:            abre la ventana de Configuracion.
# (No usamos F12 porque ya lo usa mic_tray.py para mute/unmute)
MOD_CTRL = 0x0002
MOD_SHIFT = 0x0004
VK_SPACE = 0x20
VK_F9 = 0x78
VK_F11 = 0x7A
HOTKEY_ID_TOGGLE = 2     # Ctrl+Shift+F11 (distinto al de MicToggle que usa 1)
HOTKEY_ID_REPASTE = 3    # Ctrl+Shift+Space
HOTKEY_ID_SETTINGS = 4   # Ctrl+Shift+F9

# ── Captura de audio ─────────────────────────────────────────────
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"

# ── Modelo de transcripcion ──────────────────────────────────────
# El modelo concreto sale de settings.model_name (tiny/base/small/medium).
# El default en settings.DEFAULTS es "small" (~470 MB, balance precision/velocidad en es).
MODEL_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
    "MicDictado", "models",
)
MODEL_LANG = "es"

# ── Historial de transcripciones ────────────────────────────────
# JSONL append-only en LOCALAPPDATA. Cuando supera settings.history_max_entries,
# se trunca dejando solo las ultimas N. Privacidad: 100% local, texto plano.
APP_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
    "MicDictado",
)
TRANSCRIPTS_FILE = os.path.join(APP_DATA_DIR, "transcripts.jsonl")

# ── LLM local (infraestructura para features futuras) ──────────
# El stack del LLM (carga + funcion de correccion con glosario) quedo armado
# durante la Fase 1, pero el "modo limpio" como feature general fue descartado
# (ver plan_escalamiento.md): con initial_prompt expandido, Whisper transcribe
# bien sin necesidad de post-procesado, y el LLM solo metia alucinaciones.
# Por eso _load_llm() NO se invoca al arrancar (no gastamos 3 GB de RAM por
# nada). Cuando se agreguen features especificas (atajos a Slack, reescribir
# como email formal, etc.), van a invocar _load_llm() de forma lazy en su
# primer uso.
LLM_MODEL_PATH = os.path.join(
    APP_DATA_DIR, "llm", "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
)
LLM_N_CTX = 2048

# ── Single-instance: kill + replace via lockfile con PID ────────
# Si ya hay una instancia corriendo, la matamos y nos quedamos con la nueva.
# Util en desarrollo (no hay que ir al Task Manager) y tambien si quedo zombie.
_LOCK_DIR = os.path.join(tempfile.gettempdir(), "MicDictado")
_LOCKFILE = os.path.join(_LOCK_DIR, "mic_dictado.pid")


def _enforce_single_instance():
    os.makedirs(_LOCK_DIR, exist_ok=True)
    if os.path.exists(_LOCKFILE):
        try:
            with open(_LOCKFILE, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            if old_pid and old_pid != os.getpid():
                # taskkill silencioso (si el PID no existe, simplemente devuelve error y sigue)
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(old_pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                )
                time.sleep(0.3)  # darle tiempo a liberar recursos (mic COM, audio device)
        except (ValueError, OSError):
            pass
    with open(_LOCKFILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    atexit.register(_cleanup_lockfile)


def _cleanup_lockfile():
    try:
        if os.path.exists(_LOCKFILE):
            with open(_LOCKFILE, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            # Solo borrar si es nuestro (no borrar el de otra instancia que nos reemplazo)
            if pid == os.getpid():
                os.remove(_LOCKFILE)
    except Exception:
        pass


_enforce_single_instance()


def get_mic_volume():
    # Copiado de mic_tray.py:29-34 (el usuario pidio no refactorizar)
    devices = AudioUtilities.GetMicrophone()
    if devices is None:
        raise RuntimeError("No se encontro microfono por defecto")
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return interface.QueryInterface(IAudioEndpointVolume)


# ── Tipeo Unicode via SendInput ─────────────────────────────────
# Usamos SendInput con KEYEVENTF_UNICODE para "tipear" el texto caracter por
# caracter. Funciona en cualquier app que acepte teclado: editores normales,
# terminales (cmd/PowerShell/Windows Terminal), terminales integradas de
# Cursor/VSCode (Electron), navegadores, etc. No toca el clipboard.
#
# Decision tomada despues de probar el approach previo (clipboard + Ctrl+V):
# en terminales el atajo es Ctrl+Shift+V y no podiamos detectar de forma
# robusta cuando estamos en terminal vs editor (Electron no expone el control
# interno a Win32). El tipeo Unicode resuelve eso universalmente.

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("union", _INPUT_UNION),
    ]


# Virtual-keys de los modificadores que pueden quedar "presionados" cuando
# el tipeo se dispara desde un hotkey (WM_HOTKEY llega con Ctrl/Shift down).
_VK_MODIFIERS = (
    0xA2,  # VK_LCONTROL
    0xA3,  # VK_RCONTROL
    0xA0,  # VK_LSHIFT
    0xA1,  # VK_RSHIFT
    0xA4,  # VK_LMENU (Alt izq)
    0xA5,  # VK_RMENU (Alt der)
    0x5B,  # VK_LWIN
    0x5C,  # VK_RWIN
)


def _release_modifiers():
    """Sintetiza key-up de Ctrl/Shift/Alt/Win.

    Necesario antes de hacer type_text_unicode disparado desde un hotkey: el
    usuario aun tiene Ctrl+Shift fisicamente pulsados, y sin esto las primeras
    letras Unicode se interpretan como atajos (Ctrl+B, Ctrl+S...) en la app
    destino y "se comen" parte del texto. SendInput de KEYUP solo afecta el
    estado logico que ve la app destino; las teclas fisicas siguen pulsadas
    hasta que el usuario las suelte de verdad.

    Solo soltamos los modificadores que estan realmente presionados: un
    key-up de Alt "suelto" (sin Alt presionado) activa el modo menu/KeyTips
    en apps como el Notepad de Win11, que despues se traga los caracteres."""
    u32 = ctypes.windll.user32
    inputs = []
    for vk in _VK_MODIFIERS:
        if not (u32.GetAsyncKeyState(vk) & 0x8000):
            continue
        inp = _INPUT()
        inp.type = _INPUT_KEYBOARD
        inp.union.ki = _KEYBDINPUT(vk, 0, _KEYEVENTF_KEYUP, 0, None)
        inputs.append(inp)
    if not inputs:
        return
    arr = (_INPUT * len(inputs))(*inputs)
    u32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def type_text_unicode(texto, batch_size=20, batch_delay=0.005):
    """Tipea texto caracter por caracter con SendInput KEYEVENTF_UNICODE.

    batch_size + batch_delay para no abrumar apps que procesan input lento.
    """
    if not texto:
        return

    inputs = []
    for ch in texto:
        code = ord(ch)
        if code > 0xFFFF:
            # Caracteres fuera del BMP (emoji, etc.) requieren surrogate pairs.
            # Para texto en español no aplica; los descartamos por simplicidad.
            continue
        # Key down
        inp_d = _INPUT()
        inp_d.type = _INPUT_KEYBOARD
        inp_d.union.ki = _KEYBDINPUT(0, code, _KEYEVENTF_UNICODE, 0, None)
        inputs.append(inp_d)
        # Key up
        inp_u = _INPUT()
        inp_u.type = _INPUT_KEYBOARD
        inp_u.union.ki = _KEYBDINPUT(
            0, code, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, None
        )
        inputs.append(inp_u)

    if not inputs:
        return

    user32 = ctypes.windll.user32
    sizeof_input = ctypes.sizeof(_INPUT)
    # Cada caracter genera 2 eventos (down + up). batch_size es chars.
    step = batch_size * 2
    for i in range(0, len(inputs), step):
        batch = inputs[i:i + step]
        arr = (_INPUT * len(batch))(*batch)
        sent = user32.SendInput(len(batch), arr, sizeof_input)
        if sent != len(batch):
            # SendInput puede ser bloqueado por UIPI (app destino corriendo
            # elevada/como admin) o por el sistema. Lo logueamos en vez de
            # fallar en silencio para poder diagnosticar.
            err = ctypes.windll.kernel32.GetLastError()
            print(
                f"[MicDictado] SendInput bloqueado: envio {sent}/{len(batch)} "
                f"eventos (GetLastError={err}). ¿La app destino corre como admin?"
            )
            return
        if i + step < len(inputs):
            time.sleep(batch_delay)


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _lerp_rgb(c1, c2, t):
    """Interpola entre dos colores RGB. t=0 -> c1, t=1 -> c2."""
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _limpiar_alucinaciones(texto):
    """Elimina rachas de puntos/puntos suspensivos que Whisper (sobre todo
    large-v3-turbo) alucina sobre tramos de silencio: 'Hola,...………….Hola'.

    Quita secuencias de 2+ caracteres '.' o '…' (con o sin espacios entre
    medio) y normaliza los espacios resultantes. Un punto simple legitimo
    de fin de oracion no se toca."""
    # 2+ puntos/elipsis consecutivos (admite espacios intercalados): fuera
    texto = re.sub(r"[.…](?:\s*[.…])+", " ", texto)
    # '…' suelto tambien es alucinacion tipica de silencio en dictado
    texto = re.sub(r"…", " ", texto)
    # Limpiar residuos: espacios multiples y espacios antes de puntuacion
    texto = re.sub(r"\s+([,.;:!?])", r"\1", texto)
    texto = re.sub(r"\s{2,}", " ", texto)
    return texto.strip()


def _has_repetitive_loop(texto):
    """Detecta loops tipicos de Whisper: misma secuencia de 3+ palabras
    repetida 3+ veces consecutivas. Patron canonico cuando el modelo
    alucina sobre silencio/ruido: '¿Me explicas las? ¿Me explicas las?
    ¿Me explicas las? ¿Me explicas las?'.

    O(n^2) sobre la cantidad de palabras; con n tipico < 100 es
    trivial. Se compara en lowercase y sin puntuacion para que '¿X?'
    matchee con 'X.' que matchee con 'x'."""
    palabras = [w.strip(".,;:¿?¡!\"'()[]") for w in texto.lower().split()]
    palabras = [w for w in palabras if w]
    if len(palabras) < 9:  # minimo: 3 palabras x 3 repeticiones
        return False
    max_ventana = min(8, len(palabras) // 3)
    for n in range(3, max_ventana + 1):
        for start in range(len(palabras) - 3 * n + 1):
            ventana = palabras[start:start + n]
            repeticiones = 1
            pos = start + n
            while pos + n <= len(palabras) and palabras[pos:pos + n] == ventana:
                repeticiones += 1
                pos += n
            if repeticiones >= 3:
                return True
    return False


class DictadoOverlay:
    """Overlay flotante con forma de pildora redondeada renderizada con Pillow.

    Layout horizontal: dot de estado a la izq + etiqueta + 10 LEDs como VU meter
    a la derecha. La pildora se logra con transparentcolor (los pixeles fuera del
    rectangulo redondeado son del color magico que Tk vuelve transparente).
    """

    # ── Geometria ────────────────────────────────────────────────
    WIDTH = 260
    HEIGHT = 32
    SUPERSAMPLE = 2
    PILL_RADIUS = 14

    # Layout interno (en px regulares, sin SS)
    DOT_CX = 18
    DOT_CY = 16
    DOT_R = 5
    HALO_R = 9    # radio externo del halo

    LABEL_X = 32
    LABEL_FONT_SIZE = 11

    LED_COUNT = 10
    LED_X0 = 145          # primer LED empieza aqui
    LED_W = 8
    LED_GAP = 2
    LED_Y0 = 11
    LED_Y1 = 21
    LED_RADIUS = 2        # esquinas redondeadas de cada segmento

    # ── Colores ──────────────────────────────────────────────────
    BG_PILL = "#1f1f1f"
    # Magenta puro: muy improbable que aparezca en el render real, sirve como
    # color clave de transparencia para -transparentcolor.
    TRANSPARENT_KEY = "#ff00ff"

    COLOR_IDLE = "#3a3a3a"
    COLOR_REC = "#dc3232"
    COLOR_TRANS = "#f0a020"
    COLOR_LOAD = "#4080e0"
    COLOR_CLEAN = "#9b59b6"  # violeta: estado limpiando (LLM corrigiendo texto)
    COLOR_REPROC = "#c0461a"   # naranja oscuro: reprocesando (loop detectado)
    COLOR_NOMATCH = "#707070"  # gris: audio no entendido (segundo intento tambien loopeo)

    LED_GREEN = "#28be5c"
    LED_YELLOW = "#f0c020"
    LED_RED = "#dc3232"
    # Mezcla LED-color con BG_PILL al 18% para "LED apagado pero presente"
    LED_OFF_MIX = 0.18

    HALO_FREQ_HZ = 1.5
    HALO_ALPHA_MIN = 0.20
    HALO_ALPHA_MAX = 0.60

    REFRESH_MS = 40

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("MicDictado")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-toolwindow", True)
        # Alpha global de la ventana (afecta toda la pildora). Mas bajo que antes
        # para sensacion glass.
        self.root.attributes("-alpha", 0.85)
        # transparentcolor: cualquier pixel exactamente igual a este color se
        # vuelve invisible. Permite esquinas redondeadas sobre cualquier fondo.
        self.root.wm_attributes("-transparentcolor", self.TRANSPARENT_KEY)
        self.root.configure(bg=self.TRANSPARENT_KEY)

        # Posicion: centro superior de la pantalla
        sw = self.root.winfo_screenwidth()
        x = (sw - self.WIDTH) // 2
        y = 8
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root, width=self.WIDTH, height=self.HEIGHT,
            highlightthickness=0, bg=self.TRANSPARENT_KEY, borderwidth=0,
        )
        self.canvas.pack()

        # Todo el render es una sola imagen Pillow que reemplazamos cada frame.
        self._image_id = self.canvas.create_image(0, 0, anchor="nw")
        self._photo = None  # mantener referencia para que Tk no la garbage-collectee

        # Cache de render: si el key no cambio, no repintamos.
        self._last_key = None

        # Fuente: Segoe UI Bold a SUPERSAMPLE x para AA al downscale.
        # Si falla, fallback al default de Pillow (mas feo pero no crashea).
        self._font = self._load_font(self.LABEL_FONT_SIZE * self.SUPERSAMPLE)

        # Precomputar colores RGB y los "off" de cada LED
        self._bg_rgb = _hex_to_rgb(self.BG_PILL)
        self._led_colors = self._precompute_led_colors()

        # Estado
        self._estado = "idle"
        self._level = 0.0
        self._level_source = None
        self._loop_running = False

        # Win11: deiconify()/lift() activan la ventana (en Win10 no pasaba con
        # overrideredirect) -> el overlay robaba el foco y el SendInput tipeaba
        # el texto en el overlay en vez de la app destino. WS_EX_NOACTIVATE
        # garantiza que esta ventana nunca pueda tomar foco.
        self._make_noactivate()

        self.set_state("idle")

    def _make_noactivate(self):
        """Aplica WS_EX_NOACTIVATE al HWND del overlay para que nunca robe el foco."""
        try:
            self.root.update_idletasks()
            u32 = ctypes.windll.user32
            hwnd = u32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE)
        except Exception as e:
            print(f"[MicDictado] no se pudo aplicar WS_EX_NOACTIVATE: {e}")

    # ── Carga de fuente ──────────────────────────────────────────

    def _load_font(self, size):
        candidates = [
            "C:/Windows/Fonts/segoeuib.ttf",  # Segoe UI Bold
            "C:/Windows/Fonts/seguisb.ttf",   # Segoe UI Semibold
            "C:/Windows/Fonts/arialbd.ttf",   # Arial Bold
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except (OSError, IOError):
                continue
        return ImageFont.load_default()

    # ── Precompute LED colors ────────────────────────────────────

    def _precompute_led_colors(self):
        """Para cada uno de los 10 LEDs, precalcula color encendido y apagado."""
        result = []
        for i in range(self.LED_COUNT):
            if i < 4:
                on = _hex_to_rgb(self.LED_GREEN)
            elif i < 7:
                on = _hex_to_rgb(self.LED_YELLOW)
            else:
                on = _hex_to_rgb(self.LED_RED)
            off = _lerp_rgb(self._bg_rgb, on, self.LED_OFF_MIX)
            result.append((on, off))
        return result

    # ── API publica ──────────────────────────────────────────────

    def bind_level_source(self, getter):
        self._level_source = getter

    def set_state(self, estado, preview=None):
        """Estados: idle, cargando, grabando, transcribiendo, limpiando,
        reprocesando, no_entendido."""
        if estado not in ("idle", "cargando", "grabando", "transcribiendo",
                          "limpiando", "reprocesando", "no_entendido"):
            estado = "idle"
        self._estado = estado

        if estado == "idle":
            self._level = 0.0
            self._loop_running = False
            self.root.withdraw()
            return

        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)

        # Lanzar loop si no estaba corriendo
        if not self._loop_running:
            self._loop_running = True
            self._refresh_loop()
        else:
            # Fuerza un repaint inmediato para que el cambio de estado se vea ya
            self._last_key = None
            self._paint()

    # ── Loop de refresco ─────────────────────────────────────────

    def _refresh_loop(self):
        if not self._loop_running:
            return
        # Leer level si estamos grabando; en otros estados no hace falta
        if self._estado == "grabando" and self._level_source is not None:
            try:
                self._level = float(self._level_source())
            except Exception:
                self._level = 0.0
        else:
            self._level = 0.0

        self._paint()
        self.root.after(self.REFRESH_MS, self._refresh_loop)

    # ── Cache + paint ────────────────────────────────────────────

    def _paint(self):
        """Renderiza si el cache key cambio. Llamado cada REFRESH_MS."""
        # Bucket del nivel a 0.05 (20 niveles) -> evita repaints excesivos
        level_bucket = round(min(1.0, max(0.0, self._level)) * 20)
        # Bucket de la fase del halo: 16 buckets/ciclo, solo cuenta si grabando
        if self._estado == "grabando":
            halo_phase = (time.time() * self.HALO_FREQ_HZ) % 1.0
            halo_bucket = round(halo_phase * 16) % 16
        else:
            halo_phase = 0.0
            halo_bucket = -1

        key = (self._estado, level_bucket, halo_bucket)
        if key == self._last_key:
            return
        self._last_key = key

        img = self._render(self._level, halo_phase)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfig(self._image_id, image=self._photo)

    # ── Render Pillow ────────────────────────────────────────────

    def _render(self, level, halo_phase):
        SS = self.SUPERSAMPLE
        W = self.WIDTH * SS
        H = self.HEIGHT * SS
        R = self.PILL_RADIUS * SS

        # Fondo del canvas = transparent_key. Lo que dibujemos encima en BG_PILL
        # u otro color sera lo unico visible.
        img = Image.new("RGB", (W, H), self.TRANSPARENT_KEY)
        d = ImageDraw.Draw(img)

        # ── Pildora (rect redondeado) ────────────────────────
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=self.BG_PILL)

        # ── Color y texto del estado ─────────────────────────
        state_color_hex, label_text = {
            "cargando":      (self.COLOR_LOAD,    "Cargando modelo..."),
            "grabando":      (self.COLOR_REC,     "Grabando..."),
            "transcribiendo":(self.COLOR_TRANS,   "Transcribiendo..."),
            "limpiando":     (self.COLOR_CLEAN,   "Limpiando..."),
            "reprocesando":  (self.COLOR_REPROC,  "Reprocesando..."),
            "no_entendido":  (self.COLOR_NOMATCH, "Audio no entendido"),
        }.get(self._estado, (self.COLOR_IDLE, ""))
        state_color = _hex_to_rgb(state_color_hex)

        # ── Halo pulsante (solo grabando) ────────────────────
        cx = self.DOT_CX * SS
        cy = self.DOT_CY * SS
        if self._estado == "grabando":
            # alpha visible oscila como semi-seno entre min y max
            t = 0.5 + 0.5 * math.sin(halo_phase * 2 * math.pi)
            alpha = self.HALO_ALPHA_MIN + (self.HALO_ALPHA_MAX - self.HALO_ALPHA_MIN) * t
            halo_color = _lerp_rgb(self._bg_rgb, state_color, alpha)
            hr = self.HALO_R * SS
            d.ellipse((cx - hr, cy - hr, cx + hr, cy + hr), fill=halo_color)

        # ── Dot solido ───────────────────────────────────────
        dr = self.DOT_R * SS
        d.ellipse((cx - dr, cy - dr, cx + dr, cy + dr), fill=state_color)

        # ── Texto ────────────────────────────────────────────
        if label_text:
            d.text(
                (self.LABEL_X * SS, H // 2),
                label_text,
                fill=(255, 255, 255),
                font=self._font,
                anchor="lm",
            )

        # ── LEDs ─────────────────────────────────────────────
        # Solo "vivos" en grabando; en otros estados los mostramos apagados (silueta)
        active_level = level if self._estado == "grabando" else 0.0
        for i, (on_color, off_color) in enumerate(self._led_colors):
            x_left = (self.LED_X0 + i * (self.LED_W + self.LED_GAP)) * SS
            x_right = x_left + self.LED_W * SS
            y_top = self.LED_Y0 * SS
            y_bot = self.LED_Y1 * SS
            # Threshold: el LED i se enciende cuando level >= (i+1)/LED_COUNT
            on = active_level >= (i + 1) / self.LED_COUNT
            color = on_color if on else off_color
            d.rounded_rectangle(
                (x_left, y_top, x_right, y_bot),
                radius=self.LED_RADIUS * SS,
                fill=color,
            )

        # Downscale con LANCZOS para AA limpio
        if SS != 1:
            img = img.resize((self.WIDTH, self.HEIGHT), Image.LANCZOS)
        return img


class MicDictado:
    def __init__(self):
        self.overlay = DictadoOverlay()
        self.grabando = False

        # COM del microfono
        self.volume = get_mic_volume()
        self._mute_previo = None
        self._audio_buffer = []
        self._buffer_lock = threading.Lock()
        self._stream = None

        # Nivel actual del audio para el VU meter (asignacion atomica, sin lock)
        self._current_level = 0.0
        self.overlay.bind_level_source(lambda: self._current_level)

        # Ducking de musica: bajamos volumen en _start_recording, restauramos
        # apenas termina la transcripcion.
        self.ducker = AudioDucker()

        # Ultimo texto transcripto (auto-pegado siempre, pero queda guardado por si
        # el usuario quiere re-pegar con Ctrl+Shift+Space en otro lugar)
        self._last_text = None

        # Modelo Whisper: carga en hilo daemon para no bloquear el arranque de Tk.
        # La primera vez descarga ~470 MB a MODEL_DIR. Luego queda cacheado.
        self.model = None
        self._loaded_model_name = None
        self._loaded_device = None
        self._loaded_compute_type = None
        self._model_ready = False
        self.overlay.set_state("cargando")
        threading.Thread(target=self._load_model, daemon=True).start()

        # LLM local: infraestructura lista pero NO se carga al arranque.
        # Cuando se agreguen features que lo necesiten, deberan invocar
        # _load_llm() la primera vez (carga lazy). Mantener estos atributos
        # para que _correct_with_glossary y futuras funciones puedan
        # consultarlos sin if-around-everything.
        self.llm = None
        self._llm_ready = False

        # Hotkey en hilo daemon (mismo patron que mic_tray.py:103-109)
        self._hotkey_thread = threading.Thread(target=self._listen_hotkey, daemon=True)
        self._hotkey_thread.start()

    def _resolve_device_compute(self):
        """Resuelve device y compute_type efectivos a usar.

        - 'auto' device -> 'cuda' si hay GPU detectada por ctranslate2, sino 'cpu'.
        - 'cuda' explicito sin GPU disponible -> warning y se respeta tal cual
          (la carga real va a fallar y caer al fallback de _load_model).
        - 'auto' compute_type -> 'float16' en CUDA, 'int8' en CPU (defaults sanos)."""
        requested_device = settings.device
        requested_ct = settings.compute_type
        cuda_available = False
        try:
            import ctranslate2
            cuda_available = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            cuda_available = False

        if requested_device == "auto":
            device = "cuda" if cuda_available else "cpu"
        else:
            device = requested_device

        if requested_ct == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        else:
            compute_type = requested_ct

        return device, compute_type

    def _load_model(self):
        try:
            os.makedirs(MODEL_DIR, exist_ok=True)
            model_name = settings.model_name
            device, compute_type = self._resolve_device_compute()
            print(f"[MicDictado] cargando modelo '{model_name}' device={device} compute_type={compute_type} ({MODEL_DIR})...")
            t0 = time.time()
            try:
                self.model = WhisperModel(
                    model_name, device=device, compute_type=compute_type,
                    download_root=MODEL_DIR,
                )
                self._loaded_device = device
                self._loaded_compute_type = compute_type
            except Exception as gpu_err:
                # Fallback transparente: si pidieron CUDA y fallo (driver, VRAM,
                # compute_type no soportado), seguimos con CPU+int8 que es el
                # combo que ya sabemos que anda. Asi el dictado no queda muerto
                # por un cambio de Settings o por que arranco Ollama y nos
                # quedamos sin VRAM.
                if device == "cuda":
                    print(f"[MicDictado] WARNING: fallo cargando en CUDA ({gpu_err}); fallback a CPU+int8")
                    self.model = WhisperModel(
                        model_name, device="cpu", compute_type="int8",
                        download_root=MODEL_DIR,
                    )
                    self._loaded_device = "cpu"
                    self._loaded_compute_type = "int8"
                else:
                    raise
            self._loaded_model_name = model_name

            # Pre-warmup: corre una transcripcion dummy de 0.5s de silencio
            # para que CTranslate2 compile / cachee los kernels (sobre todo en
            # CUDA, donde la primera invocacion paga la compilacion JIT). Sin
            # esto, la PRIMERA dictada del usuario paga ese costo (~0.3-1s
            # extra que sorprende). Con warmup, "modelo listo" significa
            # "listo Y caliente": la primera dictada real se siente igual de
            # rapida que la 5ta.
            try:
                warmup_audio = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
                t_warm = time.time()
                segs, _ = self.model.transcribe(
                    warmup_audio, language=MODEL_LANG, beam_size=1
                )
                # transcribe devuelve un generator; consumirlo dispara el
                # encode + decode reales que es donde compilan los kernels.
                list(segs)
                print(f"[MicDictado] pre-warmup en {time.time() - t_warm:.2f}s")
            except Exception as warm_err:
                # Best-effort: si el warmup falla, seguimos. La primera
                # transcripcion real va a hacer el trabajo igual, solo que
                # con un toque de penalty perceptible esa unica vez.
                print(f"[MicDictado] pre-warmup fallo (no critico): {warm_err}")

            self._model_ready = True
            print(
                f"[MicDictado] modelo listo en {time.time() - t0:.1f}s "
                f"(device={self._loaded_device}, compute_type={self._loaded_compute_type})"
            )
            self.overlay.root.after(0, self.overlay.set_state, "idle")
        except Exception as e:
            print(f"[MicDictado] error cargando modelo: {e}")
            import traceback
            traceback.print_exc()

    def _load_llm(self):
        """Carga Llama 3.2 3B Q4 para modo limpio. Best-effort: si falla,
        el modo limpio queda deshabilitado y el resto sigue funcionando."""
        if not os.path.exists(LLM_MODEL_PATH):
            print(f"[MicDictado] LLM no encontrado en {LLM_MODEL_PATH}")
            print("[MicDictado] modo limpio deshabilitado (Insert -> fallback a crudo)")
            return
        try:
            from llama_cpp import Llama
        except ImportError:
            print("[MicDictado] llama_cpp no instalado, modo limpio deshabilitado")
            return
        try:
            print(f"[MicDictado] cargando LLM Llama 3.2 3B Q4 ({LLM_MODEL_PATH})...")
            t0 = time.time()
            self.llm = Llama(
                model_path=LLM_MODEL_PATH,
                n_ctx=LLM_N_CTX,
                n_threads=os.cpu_count(),
                n_gpu_layers=-1,  # ignorado si la build no tiene CUDA
                verbose=False,
            )
            self._llm_ready = True
            print(f"[MicDictado] LLM listo en {time.time() - t0:.1f}s")
        except Exception as e:
            print(f"[MicDictado] error cargando LLM: {e}")
            # No es critico: el dictado crudo sigue funcionando.

    def _correct_with_glossary(self, texto):
        """Pasa el texto crudo de Whisper por el LLM con prompt estricto.

        El glosario sale de settings.initial_prompt (mismo vocabulario que
        sesga a Whisper). El prompt prohibe explicitamente reescritura,
        agregados o reformulacion: solo correccion de terminos y tildes.

        Devuelve (texto_final, ok). Si el LLM no esta listo o falla,
        ok=False y texto_final=texto_crudo (fallback transparente).

        Anti-patron prevenido: modelos pequenos (3B) tienden a leer el
        system prompt como 'instrucciones para el siguiente turno' y
        responden con saludos / pedidos de aclaracion. Para evitarlo:
          - el system aclara explicitamente que la respuesta DEBE ser
            unicamente el texto corregido, sin saludos ni preguntas;
          - el user message lleva un prefijo 'Texto a corregir:' que
            elimina ambiguedad sobre que es input."""
        if not self._llm_ready or self.llm is None:
            return texto, False
        glosario = (settings.initial_prompt or "").strip() or "(sin glosario configurado)"
        system = (
            "Sos un corrector automatico de transcripciones de voz en espanol. "
            "El usuario te envia UN solo mensaje con el texto a corregir. "
            "Tu unica respuesta es ese mismo texto, ya corregido.\n"
            "\n"
            "Tarea:\n"
            "1. Corregir terminos del glosario que esten mal escritos.\n"
            "2. Aplicar tildes y puntuacion que falten claramente.\n"
            "3. Si no hay nada para corregir, devolver el texto IDENTICO.\n"
            "\n"
            "REGLAS ABSOLUTAS:\n"
            "- NO saludes, NO te presentes, NO hagas preguntas.\n"
            "- NO pidas mas informacion. El glosario ya esta abajo.\n"
            "- NO reescribas, NO reformules, NO cambies el tono.\n"
            "- NO agregues palabras o ideas que no esten en el original.\n"
            "- NO cambies el orden de las ideas, NO completes pensamientos.\n"
            "- NO toques los numeros: '4.2', '1.8', '3.2' van con punto, NO con coma.\n"
            "- NO uses comillas, backticks, ni prefijos como 'Texto corregido:'.\n"
            "- Si dudas, no toques: devolve el texto tal cual.\n"
            "\n"
            "EJEMPLO 1\n"
            "Entrada:\n"
            "Reunion del 14 de marzo. Martin Beshi presento Mic Dictada a Santex Group. Vamos a usar Whisper con Lama 3.2 y Tool Calling.\n"
            "Salida:\n"
            "Reunión del 14 de marzo. Martin Belli presentó MicDictado a Santex Group. Vamos a usar Whisper con Llama 3.2 y tool calling.\n"
            "\n"
            "EJEMPLO 2 (sin cambios necesarios)\n"
            "Entrada:\n"
            "che, llegue tarde, te aviso cuando salgo.\n"
            "Salida:\n"
            "Che, llegué tarde, te aviso cuando salgo.\n"
            "\n"
            "EJEMPLO 3 (mantener numeros)\n"
            "Entrada:\n"
            "La latencia bajo de 4.2 a 1.8 segundos en una RTX 2060.\n"
            "Salida:\n"
            "La latencia bajó de 4.2 a 1.8 segundos en una RTX 2060.\n"
            "\n"
            f"Glosario de terminos correctos:\n{glosario}"
        )
        # Prefijo en el user message: elimina la ambiguedad de 'es esto el texto
        # o un saludo'. Tambien sirve como ancla para que el modelo no incluya
        # el prefijo en su respuesta (sabe que eso es input, no output).
        user_msg = f"Texto a corregir:\n{texto}"
        try:
            t0 = time.time()
            # Presupuesto de tokens: tamano del input + margen razonable.
            # Aprox 1 token ~= 4 caracteres en espanol.
            max_tokens = max(128, int(len(texto) / 3) + 80)
            out = self.llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            respuesta = out["choices"][0]["message"]["content"].strip()
            # Cleanup: el modelo a veces envuelve la salida en comillas o backticks
            # pese a la instruccion. Pelamos esos casos comunes.
            if len(respuesta) >= 2 and respuesta[0] == respuesta[-1] and respuesta[0] in ('"', "'"):
                respuesta = respuesta[1:-1].strip()
            if respuesta.startswith("```") and respuesta.endswith("```"):
                respuesta = respuesta.strip("`").strip()
            # Defensa adicional: si el modelo igual respondio con prefijo
            # 'Texto corregido:' o similar, quitarlo.
            for prefijo in ("Texto corregido:", "Texto a corregir:", "Correccion:", "Corrección:"):
                if respuesta.lower().startswith(prefijo.lower()):
                    respuesta = respuesta[len(prefijo):].strip()
                    break
            elapsed = time.time() - t0
            print(f"[MicDictado] correccion LLM ({elapsed:.1f}s): {respuesta!r}")
            return respuesta or texto, True
        except Exception as e:
            print(f"[MicDictado] error en correccion LLM: {e}")
            return texto, False

    def _log_transcript(self, texto, duracion_s, elapsed_s, initial_prompt):
        """Appendea una linea JSON a transcripts.jsonl. Si supera el limite,
        trunca dejando solo las ultimas N entradas.

        Diseñado para ser robusto: cualquier error de I/O se loggea y se ignora,
        nunca debe romper el flujo de transcripcion."""
        try:
            os.makedirs(APP_DATA_DIR, exist_ok=True)
            entry = {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "model": self._loaded_model_name or settings.model_name,
                "beam_size": settings.beam_size,
                "vad_filter": bool(settings.vad_filter),
                "duracion_s": round(float(duracion_s), 2),
                "elapsed_s": round(float(elapsed_s), 2),
                "initial_prompt": initial_prompt or "",
                "texto": texto,
            }
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with open(TRANSCRIPTS_FILE, "a", encoding="utf-8") as f:
                f.write(line)

            # Rotacion: si supera el limite, releer todo y truncar.
            # Aceptamos el costo O(N) porque N tipico es 500 = ~lineas, nada.
            max_entries = max(1, int(settings.history_max_entries))
            with open(TRANSCRIPTS_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > max_entries:
                kept = lines[-max_entries:]
                tmp = TRANSCRIPTS_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                os.replace(tmp, TRANSCRIPTS_FILE)
        except OSError as e:
            print(f"[MicDictado] error escribiendo historial: {e}")

    def reload_model_if_needed(self):
        """Si model_name/device/compute_type cambiaron, recarga el modelo en background.

        Compara contra los valores efectivos cargados (post-resolucion 'auto'),
        no contra el string crudo de settings: asi un cambio cosmetico de
        'auto' -> 'cuda' (cuando ya estabamos en cuda por auto) no dispara
        reload innecesario."""
        target_device, target_ct = self._resolve_device_compute()
        if (
            getattr(self, "_loaded_model_name", None) == settings.model_name
            and getattr(self, "_loaded_device", None) == target_device
            and getattr(self, "_loaded_compute_type", None) == target_ct
        ):
            return
        self._model_ready = False
        self.overlay.root.after(0, self.overlay.set_state, "cargando")
        threading.Thread(target=self._load_model, daemon=True).start()

    def _com_call(self, accion):
        """Ejecuta accion(self.volume) con retry tras fallo de COM.
        Mismo patron que mic_tray.py:132-142 para sobrevivir a sleep/USB reconnect."""
        try:
            return accion(self.volume)
        except Exception:
            self.volume = get_mic_volume()
            return accion(self.volume)

    def _listen_hotkey(self):
        """Escucha hotkeys: F11 toggle, Shift+Space re-pegar ultimo, F9 abrir Settings."""
        u32 = ctypes.windll.user32
        # Registramos los 3 hotkeys y avisamos si alguno fallo (otra app lo tomo).
        # RegisterHotKey devuelve 0 si la combinacion ya esta tomada globalmente.
        for hid, vk, label in (
            (HOTKEY_ID_TOGGLE, VK_F11, "Ctrl+Shift+F11 (toggle)"),
            (HOTKEY_ID_REPASTE, VK_SPACE, "Ctrl+Shift+Space (re-pegar)"),
            (HOTKEY_ID_SETTINGS, VK_F9, "Ctrl+Shift+F9 (settings)"),
        ):
            ok = u32.RegisterHotKey(None, hid, MOD_CTRL | MOD_SHIFT, vk)
            if not ok:
                print(f"[MicDictado] WARNING: hotkey {label} ya esta tomado por otra app, no funcionara")
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            if msg.message != 0x0312:  # WM_HOTKEY
                continue
            hid = msg.wParam
            if hid == HOTKEY_ID_TOGGLE:
                self.overlay.root.after(0, self._toggle)
            elif hid == HOTKEY_ID_REPASTE:
                self.overlay.root.after(0, self._repaste_last)
            elif hid == HOTKEY_ID_SETTINGS:
                self.overlay.root.after(0, self._open_settings_via_hotkey)

    def _open_settings_via_hotkey(self):
        """Abre la ventana de Configuracion desde el hotkey, sin depender del tray."""
        try:
            from tray import SettingsWindow
            existing = getattr(self, "_settings_window", None)
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
            self._settings_window = SettingsWindow(self)
        except Exception as e:
            print(f"[MicDictado] error abriendo Settings: {e}")
            import traceback
            traceback.print_exc()

    def _toggle(self):
        """F11: alterna grabacion."""
        if not self._model_ready:
            print("[MicDictado] modelo aun cargando, ignorando disparo")
            self.overlay.set_state("cargando")
            return
        if self.grabando:
            self._stop_recording()
        else:
            self._start_recording()

    def _repaste_last(self):
        """Ctrl+Shift+Space: re-pega el ultimo texto transcripto donde este el cursor.

        El _last_text NO se borra: se puede re-pegar varias veces en distintos lugares."""
        if not self._last_text:
            print("[MicDictado] no hay texto previo para re-pegar")
            return
        print(f"[MicDictado] re-pegando ultimo texto ({len(self._last_text)} chars)")
        # Soltar Ctrl+Shift virtualmente: el WM_HOTKEY llego con esos modificadores
        # fisicamente pulsados, y sin esto las primeras letras se interpretan como
        # atajos (Ctrl+B, Ctrl+S...) en la app destino y se "come" parte del texto.
        _release_modifiers()
        # Pequeno delay para que la app destino procese el key-up antes de recibir
        # los caracteres Unicode.
        self.overlay.root.after(60, lambda: type_text_unicode(self._last_text))

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback de sounddevice: appendea chunk int16 al buffer y actualiza VU meter."""
        if status:
            print(f"[audio] status: {status}")
        with self._buffer_lock:
            self._audio_buffer.append(indata.copy())

        # Calcular RMS del chunk (normalizado int16 -> float [0,1]) para el VU meter.
        # La ganancia es configurable (settings.vu_gain). Voz normal da RMS ~0.05-0.15;
        # con default 7.0 la barra llega a verde alto sin gritar.
        chunk_f = indata.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(chunk_f ** 2)))
        self._current_level = min(rms * settings.vu_gain, 1.0)

    def _start_recording(self):
        try:
            # Guardar estado mute y desmutear si hace falta
            self._mute_previo = bool(self._com_call(lambda v: v.GetMute()))
            if self._mute_previo:
                self._com_call(lambda v: v.SetMute(False, None))

            # Bajar volumen de Spotify/YouTube/etc. mientras dictamos
            try:
                self.ducker.duck()
            except Exception as e:
                print(f"[MicDictado] error al duckear: {e}")

            # Buffer limpio y abrir stream
            with self._buffer_lock:
                self._audio_buffer = []

            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=self._audio_callback,
            )
            self._stream.start()

            self.grabando = True
            self.overlay.set_state("grabando")
            print("[MicDictado] grabando...")
        except Exception as e:
            print(f"[MicDictado] error al iniciar grabacion: {e}")
            self.overlay.set_state("idle")
            try:
                self.ducker.restore()
            except Exception:
                pass

    def _stop_recording(self):
        try:
            # Cerrar stream
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None

            # Restaurar mute YA, antes de transcribir. La transcripcion no necesita
            # el mic abierto y el usuario espera que el mic vuelva a su estado
            # apenas suelta el hotkey (no quiere quedar abierto 3-5s durante el STT).
            try:
                if self._mute_previo:
                    self._com_call(lambda v: v.SetMute(True, None))
            except Exception as e:
                print(f"[MicDictado] error restaurando mute: {e}")

            # Restaurar volumen de musica YA: el usuario solto el hotkey y quiere
            # escuchar de nuevo mientras la transcripcion corre en background.
            try:
                self.ducker.restore()
            except Exception as e:
                print(f"[MicDictado] error al restaurar ducking: {e}")

            # Procesar buffer en hilo separado para no bloquear Tk
            threading.Thread(target=self._procesar_audio, daemon=True).start()
        except Exception as e:
            print(f"[MicDictado] error al detener grabacion: {e}")
        finally:
            self.grabando = False

    def _transcribir(self, audio_f32, initial_prompt, beam_size, vad_filter,
                      condition_on_previous_text=True):
        """Llama a model.transcribe y devuelve (texto_unido, elapsed_s).

        Aislado para usarse tanto en el primer pase (parametros del usuario)
        como en el reprocesamiento defensivo (parametros mas conservadores)
        sin duplicar codigo."""
        t0 = time.time()
        segments, _ = self.model.transcribe(
            audio_f32,
            language=MODEL_LANG,
            beam_size=beam_size,
            vad_filter=vad_filter,
            initial_prompt=initial_prompt,
            condition_on_previous_text=condition_on_previous_text,
        )
        # segments es un generator; consumirlo materializa la transcripcion.
        # Filtramos segmentos que el propio modelo marca como probable
        # silencio (no_speech_prob alto): son la fuente de las alucinaciones
        # tipo '…………' que turbo genera sobre pausas entre frases.
        partes = []
        for seg in segments:
            txt = seg.text.strip()
            if not txt:
                continue
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8:
                print(
                    f"[MicDictado] segmento descartado (no_speech={seg.no_speech_prob:.2f}, "
                    f"logprob={seg.avg_logprob:.2f}): {txt!r}"
                )
                continue
            partes.append(txt)
        texto = _limpiar_alucinaciones(" ".join(partes))
        return texto, time.time() - t0

    def _procesar_audio(self):
        """Transcribe el buffer y pega. Si el primer intento devuelve un
        loop repetitivo (patron tipico de Whisper bajo presion de memoria
        o con audio degradado), reprocesa el mismo audio con beam_size=5
        y vad_filter=False (mas robusto contra alucinaciones). Si el
        segundo intento tambien loopea, descarta sin pegar y avisa
        visualmente con 'Audio no entendido' por 2s."""
        self.overlay.root.after(0, self.overlay.set_state, "transcribiendo")
        # Si descartamos por loop persistente programamos el idle nosotros
        # con un after(2000); en ese caso el finally NO debe sobreescribir.
        overlay_diferido = False
        try:
            with self._buffer_lock:
                chunks = list(self._audio_buffer)
                self._audio_buffer = []

            if not chunks:
                print("[MicDictado] buffer vacio, nada que hacer")
                return

            # int16 [N, 1] -> float32 [N] normalizado a [-1, 1], que es lo que espera Whisper
            audio_int16 = np.concatenate(chunks, axis=0).squeeze()
            duracion = len(audio_int16) / SAMPLE_RATE
            audio_f32 = audio_int16.astype(np.float32) / 32768.0

            if duracion < 0.3:
                print(f"[MicDictado] audio muy corto ({duracion:.2f}s), descartando")
                return

            print(f"[MicDictado] transcribiendo {duracion:.2f}s de audio...")
            initial_prompt = (settings.initial_prompt or "").strip() or None

            # Primer pase con los parametros configurados por el usuario
            texto, elapsed = self._transcribir(
                audio_f32, initial_prompt,
                beam_size=settings.beam_size,
                vad_filter=settings.vad_filter,
            )
            print(f"[MicDictado] transcripcion ({elapsed:.1f}s): {texto!r}")

            # Deteccion de loop + reprocesamiento defensivo.
            # beam_size=5: beam search corta loops que greedy (beam=1) deja pasar.
            # vad_filter=False: el VAD a veces le pasa al modelo chunks raros que
            #   son los que originan el loop; mejor mandarle el audio entero.
            # condition_on_previous_text=False: previene que el primer segmento
            #   defectuoso contagie a los siguientes via condicionamiento autoregresivo.
            if texto and _has_repetitive_loop(texto):
                print("[MicDictado] loop repetitivo detectado, reprocesando con beam=5...")
                self.overlay.root.after(0, self.overlay.set_state, "reprocesando")
                texto, elapsed2 = self._transcribir(
                    audio_f32, initial_prompt,
                    beam_size=5, vad_filter=False,
                    condition_on_previous_text=False,
                )
                elapsed += elapsed2
                print(f"[MicDictado] reprocesamiento ({elapsed2:.1f}s): {texto!r}")

                if _has_repetitive_loop(texto):
                    # El loop persiste -> el audio realmente esta degradado
                    # (RAM swappeada, ruido fuerte, etc.). Descartamos y
                    # avisamos para que el usuario hable de nuevo.
                    print("[MicDictado] loop persiste tras reprocesar, descartando")
                    self.overlay.root.after(0, self.overlay.set_state, "no_entendido")
                    self.overlay.root.after(2000, lambda: self.overlay.set_state("idle"))
                    overlay_diferido = True
                    return

            # Persistir en historial (solo si esta habilitado y hay texto util)
            if texto and settings.history_enabled:
                self._log_transcript(
                    texto=texto,
                    duracion_s=duracion,
                    elapsed_s=elapsed,
                    initial_prompt=initial_prompt,
                )

            # NOTA: el restore del ducking ya se hizo en _stop_recording, apenas
            # el usuario solto el hotkey. Aca no hace falta tocarlo en el caso happy path.

            if not texto:
                return

            # Auto-pegado donde este el cursor (SendInput Unicode).
            # Guardamos el texto en _last_text por si el usuario quiere re-pegarlo
            # con Ctrl+Shift+Space (caso tipico: perdio el foco antes del pegado).
            self._last_text = texto
            # Igual que en _repaste_last: el stop llego via Ctrl+Shift+F11 y el
            # usuario puede seguir teniendo Ctrl/Shift fisicamente pulsados
            # (la transcripcion tarda <1s). Sin el release, los caracteres
            # Unicode se interpretan como atajos/chars de control en la app
            # destino y el texto se pega corrupto ('?????', letras comidas).
            _release_modifiers()
            time.sleep(0.06)
            type_text_unicode(texto)
        except Exception as e:
            print(f"[MicDictado] error procesando audio: {e}")
            import traceback
            traceback.print_exc()
            # Defensivo: si por alguna razon llegamos aca con la musica todavia
            # baja (ej. crash entre _stop_recording y aca), restauramos.
            try:
                self.ducker.restore()
            except Exception:
                pass
        finally:
            if not overlay_diferido:
                self.overlay.root.after(0, self.overlay.set_state, "idle")

    def run(self):
        self.overlay.root.mainloop()


if __name__ == "__main__":
    try:
        app = MicDictado()

        # Tray icon en hilo daemon (menu Toggle / Configuracion / Salir)
        from tray import TrayIcon
        tray = TrayIcon(app)
        tray.start()

        print("MicDictado iniciado.")
        print("  Ctrl+Shift+F11    = toggle grabacion (auto-pega al terminar)")
        print("  Ctrl+Shift+Space  = re-pegar el ultimo texto donde este el cursor")
        print("  Ctrl+Shift+F9     = abrir ventana de Configuracion")
        app.run()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        input("Presiona Enter para cerrar...")
