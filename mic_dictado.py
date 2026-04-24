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
import wave
import tkinter as tk
from ctypes import wintypes

import numpy as np
import sounddevice as sd
from comtypes import CLSCTX_ALL
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


class DictadoOverlay:
    """Ventana pequena flotante con indicador de estado del dictado."""

    BG = "#1f1f1f"
    COLOR_IDLE = "#3a3a3a"
    COLOR_REC = "#dc3232"
    COLOR_TRANS = "#f0a020"
    COLOR_LOAD = "#4080e0"
    WIDTH = 180
    HEIGHT = 36

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

        self.set_state("idle")

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

        # En idle ocultamos la ventana para no distraer
        if estado == "idle":
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)


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

        # Hotkey en hilo daemon (mismo patron que mic_tray.py:103-109)
        self._hotkey_thread = threading.Thread(target=self._listen_hotkey, daemon=True)
        self._hotkey_thread.start()

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
        """Alterna grabacion."""
        if self.grabando:
            self._stop_recording()
        else:
            self._start_recording()

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback de sounddevice: appendea chunk int16 al buffer."""
        if status:
            print(f"[audio] status: {status}")
        with self._buffer_lock:
            self._audio_buffer.append(indata.copy())

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

            # Procesar buffer en hilo separado para no bloquear Tk
            threading.Thread(target=self._procesar_audio, daemon=True).start()
        except Exception as e:
            print(f"[MicDictado] error al detener grabacion: {e}")
        finally:
            self.grabando = False

    def _procesar_audio(self):
        """En fase 2: guarda WAV temporal para inspeccion manual."""
        self.overlay.root.after(0, self.overlay.set_state, "transcribiendo")
        try:
            with self._buffer_lock:
                chunks = list(self._audio_buffer)
                self._audio_buffer = []

            if not chunks:
                print("[MicDictado] buffer vacio, nada que hacer")
                return

            audio_int16 = np.concatenate(chunks, axis=0)
            duracion = len(audio_int16) / SAMPLE_RATE

            # Guardar WAV temporal (fase 2 solo inspeccion)
            tmp_dir = os.path.join(tempfile.gettempdir(), "MicDictado")
            os.makedirs(tmp_dir, exist_ok=True)
            wav_path = os.path.join(tmp_dir, f"dictado_{int(time.time())}.wav")
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(2)  # int16 = 2 bytes
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(audio_int16.tobytes())

            print(f"[MicDictado] duracion: {duracion:.2f}s | WAV: {wav_path}")
        except Exception as e:
            print(f"[MicDictado] error procesando audio: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Restaurar mute al estado previo
            try:
                if self._mute_previo:
                    self._com_call(lambda v: v.SetMute(True, None))
            except Exception as e:
                print(f"[MicDictado] error restaurando mute: {e}")
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
