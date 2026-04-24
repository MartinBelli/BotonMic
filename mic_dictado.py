"""
mic_dictado.py — Dictado voice-to-text para Windows.

Hotkey global Ctrl+Shift+F11 (toggle): primer disparo empieza a grabar,
segundo disparo termina, transcribe con faster-whisper y pega donde este
el cursor. Si el mic esta muteado lo desmutea automaticamente y restaura
el estado previo al terminar.

Requiere: pip install -r requirements.txt
"""

import ctypes
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

# ── Hotkey global: Ctrl+Shift+F11 ──────────────────────────────
MOD_CTRL = 0x0002
MOD_SHIFT = 0x0004
VK_F11 = 0x7A
HOTKEY_ID = 2  # distinto al de MicToggle (que usa 1)

# ── Mutex: impedir multiples instancias ──────────────────────────
_mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Global\\MicDictadoMutex")
if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    ctypes.windll.user32.MessageBoxW(
        0, "MicDictado ya esta corriendo.", "MicDictado", 0x40
    )
    sys.exit(0)


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

        # Hotkey en hilo daemon (mismo patron que mic_tray.py:103-109)
        self._hotkey_thread = threading.Thread(target=self._listen_hotkey, daemon=True)
        self._hotkey_thread.start()

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
        """Alterna grabacion. En Fase 1 solo cambia estado visual."""
        if self.grabando:
            self.grabando = False
            self.overlay.set_state("idle")
            print("[MicDictado] stop (fase 1: sin audio todavia)")
        else:
            self.grabando = True
            self.overlay.set_state("grabando")
            print("[MicDictado] start (fase 1: sin audio todavia)")

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
