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
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from ctypes import wintypes

import numpy as np
import sounddevice as sd
from comtypes import CLSCTX_ALL
from faster_whisper import WhisperModel
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

# ── Hotkey global: Ctrl+Shift+F11 ──────────────────────────────
MOD_CTRL = 0x0002
MOD_SHIFT = 0x0004
VK_F11 = 0x7A
HOTKEY_ID = 2  # distinto al de MicToggle (que usa 1)

# ── Captura de audio ─────────────────────────────────────────────
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"

# ── Modelo de transcripcion ──────────────────────────────────────
MODEL_NAME = "small"  # ~470 MB descargado, decente en español
MODEL_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
    "MicDictado", "models",
)
MODEL_LANG = "es"

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
        user32.SendInput(len(batch), arr, sizeof_input)
        if i + step < len(inputs):
            time.sleep(batch_delay)


class DictadoOverlay:
    """Ventana pequena flotante con indicador de estado y VU meter."""

    BG = "#1f1f1f"
    COLOR_IDLE = "#3a3a3a"
    COLOR_REC = "#dc3232"
    COLOR_TRANS = "#f0a020"
    COLOR_LOAD = "#4080e0"
    METER_BG = "#2a2a2a"
    WIDTH = 260
    HEIGHT = 36

    # Layout del meter
    METER_X0 = 150
    METER_X1 = 250
    METER_Y0 = 13
    METER_Y1 = 23

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("MicDictado")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-toolwindow", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg=self.BG)

        # Posicion: centro superior de la pantalla
        sw = self.root.winfo_screenwidth()
        x = (sw - self.WIDTH) // 2
        y = 8
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root, width=self.WIDTH, height=self.HEIGHT,
            highlightthickness=0, bg=self.BG,
        )
        self.canvas.pack()

        self._dot = self.canvas.create_oval(10, 12, 26, 28, fill=self.COLOR_IDLE, outline="")
        self._label = self.canvas.create_text(
            36, self.HEIGHT // 2, anchor="w",
            text="Listo", fill="white", font=("Segoe UI", 10, "bold"),
        )

        # VU meter: fondo + barra de fill que crece con el nivel
        self._meter_bg = self.canvas.create_rectangle(
            self.METER_X0, self.METER_Y0, self.METER_X1, self.METER_Y1,
            fill=self.METER_BG, outline="",
        )
        self._meter_fill = self.canvas.create_rectangle(
            self.METER_X0, self.METER_Y0, self.METER_X0, self.METER_Y1,
            fill=self.COLOR_IDLE, outline="",
        )

        # Estado interno del meter
        self._level_source = None  # callable que devuelve float [0,1]
        self._meter_running = False
        self._estado = "idle"

        self.set_state("idle")

    def bind_level_source(self, getter):
        """Registrar una funcion callable que devuelve el nivel actual [0,1]."""
        self._level_source = getter

    def set_state(self, estado):
        """Estados: idle, cargando, grabando, transcribiendo."""
        mapping = {
            "idle": (self.COLOR_IDLE, "Listo"),
            "cargando": (self.COLOR_LOAD, "Cargando modelo..."),
            "grabando": (self.COLOR_REC, "Grabando..."),
            "transcribiendo": (self.COLOR_TRANS, "Transcribiendo..."),
        }
        color, texto = mapping.get(estado, (self.COLOR_IDLE, "Listo"))
        self.canvas.itemconfig(self._dot, fill=color)
        self.canvas.itemconfig(self._label, text=texto)
        self._estado = estado

        # En idle ocultamos la ventana para no distraer
        if estado == "idle":
            self.set_level(0.0)
            self._meter_running = False
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)

        # Solo mostramos el meter activo durante grabacion
        if estado == "grabando":
            if not self._meter_running:
                self._meter_running = True
                self._refresh_level_loop()
        else:
            self._meter_running = False
            if estado != "idle":
                self.set_level(0.0)

    def set_level(self, level):
        """level ∈ [0,1] -> redimensiona el fill y le asigna color segun el rango."""
        level = max(0.0, min(1.0, level))
        x0 = self.METER_X0
        x1 = x0 + int((self.METER_X1 - self.METER_X0) * level)
        self.canvas.coords(
            self._meter_fill, x0, self.METER_Y0, x1, self.METER_Y1,
        )

        if level < 0.05:
            color = "#3a3a3a"   # gris: casi silencio
        elif level < 0.40:
            color = "#28be5c"   # verde: OK
        elif level < 0.80:
            color = "#f0c020"   # amarillo: alto
        else:
            color = "#dc3232"   # rojo: clipping
        self.canvas.itemconfig(self._meter_fill, fill=color)

    def _refresh_level_loop(self):
        """Corre cada 40ms mientras estado == grabando, leyendo _level_source."""
        if not self._meter_running or self._level_source is None:
            return
        try:
            self.set_level(self._level_source())
        except Exception:
            pass
        self.root.after(40, self._refresh_level_loop)


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

        # Modelo Whisper: carga en hilo daemon para no bloquear el arranque de Tk.
        # La primera vez descarga ~470 MB a MODEL_DIR. Luego queda cacheado.
        self.model = None
        self._model_ready = False
        self.overlay.set_state("cargando")
        threading.Thread(target=self._load_model, daemon=True).start()

        # Hotkey en hilo daemon (mismo patron que mic_tray.py:103-109)
        self._hotkey_thread = threading.Thread(target=self._listen_hotkey, daemon=True)
        self._hotkey_thread.start()

    def _load_model(self):
        try:
            os.makedirs(MODEL_DIR, exist_ok=True)
            print(f"[MicDictado] cargando modelo '{MODEL_NAME}' en {MODEL_DIR}...")
            t0 = time.time()
            self.model = WhisperModel(
                MODEL_NAME, device="cpu", compute_type="int8",
                download_root=MODEL_DIR,
            )
            self._model_ready = True
            print(f"[MicDictado] modelo listo en {time.time() - t0:.1f}s")
            self.overlay.root.after(0, self.overlay.set_state, "idle")
        except Exception as e:
            print(f"[MicDictado] error cargando modelo: {e}")
            import traceback
            traceback.print_exc()

    def _com_call(self, accion):
        """Ejecuta accion(self.volume) con retry tras fallo de COM.
        Mismo patron que mic_tray.py:132-142 para sobrevivir a sleep/USB reconnect."""
        try:
            return accion(self.volume)
        except Exception:
            self.volume = get_mic_volume()
            return accion(self.volume)

    def _listen_hotkey(self):
        """Escucha Ctrl+Shift+F11 via RegisterHotKey."""
        ctypes.windll.user32.RegisterHotKey(
            None, HOTKEY_ID, MOD_CTRL | MOD_SHIFT, VK_F11
        )
        msg = wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            if msg.message == 0x0312:  # WM_HOTKEY
                self.overlay.root.after(0, self._toggle)

    def _toggle(self):
        """Alterna grabacion. Si el modelo aun no esta cargado, ignora el disparo."""
        if not self._model_ready:
            print("[MicDictado] modelo aun cargando, ignorando disparo")
            self.overlay.set_state("cargando")
            return
        if self.grabando:
            self._stop_recording()
        else:
            self._start_recording()

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback de sounddevice: appendea chunk int16 al buffer y actualiza VU meter."""
        if status:
            print(f"[audio] status: {status}")
        with self._buffer_lock:
            self._audio_buffer.append(indata.copy())

        # Calcular RMS del chunk (normalizado int16 -> float [0,1]) para el VU meter.
        # Multiplicamos x4 porque voz normal suele dar RMS ~0.05-0.15; asi la barra
        # tiene rango util sin que haya que gritar para llegar a amarillo.
        chunk_f = indata.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(chunk_f ** 2)))
        self._current_level = min(rms * 4.0, 1.0)

    def _start_recording(self):
        try:
            # Guardar estado mute y desmutear si hace falta
            self._mute_previo = bool(self._com_call(lambda v: v.GetMute()))
            if self._mute_previo:
                self._com_call(lambda v: v.SetMute(False, None))

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

            # Procesar buffer en hilo separado para no bloquear Tk
            threading.Thread(target=self._procesar_audio, daemon=True).start()
        except Exception as e:
            print(f"[MicDictado] error al detener grabacion: {e}")
        finally:
            self.grabando = False

    def _procesar_audio(self):
        """Transcribe el buffer capturado con faster-whisper y lo imprime."""
        self.overlay.root.after(0, self.overlay.set_state, "transcribiendo")
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
            t0 = time.time()
            segments, info = self.model.transcribe(
                audio_f32,
                language=MODEL_LANG,
                beam_size=5,
            )
            # segments es un generator; consumirlo materializa la transcripcion
            texto = " ".join(seg.text.strip() for seg in segments).strip()
            elapsed = time.time() - t0
            print(f"[MicDictado] transcripcion ({elapsed:.1f}s): {texto!r}")

            # Tipear el texto donde esta el cursor (Unicode via SendInput,
            # funciona en cualquier app incluyendo terminales)
            if texto:
                type_text_unicode(texto)
        except Exception as e:
            print(f"[MicDictado] error procesando audio: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # El mute ya fue restaurado en _stop_recording, antes de empezar a
            # transcribir, para que el remute sea inmediato al soltar el hotkey.
            self.overlay.root.after(0, self.overlay.set_state, "idle")

    def run(self):
        self.overlay.root.mainloop()


if __name__ == "__main__":
    try:
        app = MicDictado()
        print("MicDictado iniciado. Ctrl+Shift+F11 para toggle.")
        app.run()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        input("Presiona Enter para cerrar...")
