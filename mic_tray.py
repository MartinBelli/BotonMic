"""
mic_tray.py — Toggle de micrófono en la barra de tareas de Windows.
Requiere: pip install pycaw
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
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, IAudioMeterInformation

# ── Hotkey global: Ctrl+Shift+F12 ──────────────────────────────
MOD_CTRL = 0x0002
MOD_SHIFT = 0x0004
VK_F12 = 0x7B
HOTKEY_ID = 1

# ── Single-instance: kill + replace via lockfile con PID ────────
_LOCK_DIR = os.path.join(tempfile.gettempdir(), "MicToggle")
_LOCKFILE = os.path.join(_LOCK_DIR, "mic_tray.pid")


def _enforce_single_instance():
    os.makedirs(_LOCK_DIR, exist_ok=True)
    if os.path.exists(_LOCKFILE):
        try:
            with open(_LOCKFILE, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            if old_pid and old_pid != os.getpid():
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(old_pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
                time.sleep(0.3)
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
            if pid == os.getpid():
                os.remove(_LOCKFILE)
    except Exception:
        pass


_enforce_single_instance()


def get_mic_volume():
    devices = AudioUtilities.GetMicrophone()
    if devices is None:
        raise RuntimeError("No se encontró micrófono por defecto")
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return interface.QueryInterface(IAudioEndpointVolume)


def get_mic_meter():
    """Devuelve IAudioMeterInformation del mic default (peak sin abrir stream)."""
    devices = AudioUtilities.GetMicrophone()
    if devices is None:
        raise RuntimeError("No se encontró micrófono por defecto")
    interface = devices.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
    return interface.QueryInterface(IAudioMeterInformation)


def get_taskbar_rect():
    """Obtiene posición y tamaño de la barra de tareas."""
    from ctypes import wintypes
    class APPBARDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uCallbackMessage", wintypes.UINT),
            ("uEdge", wintypes.UINT),
            ("rc", wintypes.RECT),
            ("lParam", wintypes.LPARAM),
        ]
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    ctypes.windll.shell32.SHAppBarMessage(5, ctypes.byref(abd))  # ABM_GETTASKBARPOS
    return abd.rc


class MicToggle:
    TASKBAR_BG = "#1f1f1f"
    COLOR_ACTIVE = "#28be5c"
    COLOR_MUTED = "#dc3232"
    COLOR_METER_BG = "#2a2a2a"
    COLOR_METER_LOW = "#3a3a3a"
    COLOR_METER_OK = "#28be5c"
    COLOR_METER_HIGH = "#f0c020"
    COLOR_METER_PEAK = "#dc3232"
    ICON_SIZE = 32
    PADDING = 4
    METER_WIDTH = 6
    METER_GAP = 6
    # Sensibilidad del meter: multiplicador sobre el peak raw (voz normal ~0.1)
    METER_SENSITIVITY = 5.0

    # Cadencia del tick: 40 ms visual, chequeo de mute cada 12 ticks (~500 ms)
    TICK_MS = 40
    MUTE_CHECK_EVERY = 12

    def __init__(self):
        self.volume = get_mic_volume()
        self.meter = get_mic_meter()
        self.muted = bool(self.volume.GetMute())

        self.root = tk.Tk()
        self.root.title("Mic")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-toolwindow", True)
        self.root.configure(bg=self.TASKBAR_BG)

        # Tamaño del widget = icono + padding + gap + barra + padding
        w = self.ICON_SIZE + self.PADDING * 2 + self.METER_GAP + self.METER_WIDTH
        taskbar = get_taskbar_rect()
        taskbar_h = taskbar.bottom - taskbar.top

        # Centrar verticalmente en la barra de tareas
        x = taskbar.right - w - 200  # a la izquierda del systray
        y = taskbar.top + (taskbar_h - taskbar_h) // 2
        h = taskbar_h

        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root, width=w, height=h,
            highlightthickness=0, cursor="hand2", bg=self.TASKBAR_BG,
        )
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._on_click)

        # Drag para reposicionar en la barra
        self._drag_data = {}
        self.canvas.bind("<Button-3>", self._start_drag)
        self.canvas.bind("<B3-Motion>", self._do_drag)

        self._tick_count = 0
        self._draw()
        self._tick()  # arranca el loop de meter + sync de mute

        # Registrar hotkey global (Ctrl+Shift+F12) en hilo separado
        self._hotkey_thread = threading.Thread(target=self._listen_hotkey, daemon=True)
        self._hotkey_thread.start()

    def _listen_hotkey(self):
        """Escucha el hotkey global usando RegisterHotKey de Windows."""
        ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, MOD_CTRL | MOD_SHIFT, VK_F12)
        msg = wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            if msg.message == 0x0312:  # WM_HOTKEY
                self.root.after(0, self._on_click, None)

    def _draw(self):
        self.canvas.delete("all")
        color = self.COLOR_MUTED if self.muted else self.COLOR_ACTIVE
        icon_w = self.ICON_SIZE + self.PADDING * 2
        h = int(self.root.geometry().split("x")[1].split("+")[0])
        # Centrar el círculo verticalmente
        cx = icon_w // 2
        cy = h // 2
        r = self.ICON_SIZE // 2
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")
        # Micrófono (mismo dibujo original que se veia bien)
        self.canvas.create_rectangle(cx - 4, cy - 10, cx + 4, cy - 1, fill="white", outline="")
        self.canvas.create_arc(cx - 7, cy - 6, cx + 7, cy + 4, start=180, extent=180,
                               outline="white", width=2, style="arc")
        self.canvas.create_line(cx, cy + 4, cx, cy + 7, fill="white", width=2)
        self.canvas.create_line(cx - 4, cy + 7, cx + 4, cy + 7, fill="white", width=2)
        # Tachado si muteado
        if self.muted:
            self.canvas.create_line(cx - r + 4, cy - r + 4, cx + r - 4, cy + r - 4,
                                    fill="#ffff64", width=2)

        # VU meter vertical (barra continua) a la derecha del icono
        meter_x0 = icon_w + self.METER_GAP
        meter_x1 = meter_x0 + self.METER_WIDTH
        meter_top = cy - r
        meter_bot = cy + r
        self._meter_top = meter_top
        self._meter_bot = meter_bot
        self._meter_x0 = meter_x0
        self._meter_x1 = meter_x1
        self.canvas.create_rectangle(meter_x0, meter_top, meter_x1, meter_bot,
                                     fill=self.COLOR_METER_BG, outline="")
        self._meter_fill = self.canvas.create_rectangle(
            meter_x0, meter_bot, meter_x1, meter_bot,
            fill=self.COLOR_METER_LOW, outline="",
        )

    def _set_meter_level(self, level):
        """level ∈ [0,1] -> redimensiona la barra vertical y pinta segun rango."""
        level = max(0.0, min(1.0, level))
        altura_total = self._meter_bot - self._meter_top
        fill_top = self._meter_bot - int(altura_total * level)

        if level < 0.05:
            color = self.COLOR_METER_LOW
        elif level < 0.40:
            color = self.COLOR_METER_OK
        elif level < 0.80:
            color = self.COLOR_METER_HIGH
        else:
            color = self.COLOR_METER_PEAK

        try:
            self.canvas.coords(self._meter_fill,
                               self._meter_x0, fill_top,
                               self._meter_x1, self._meter_bot)
            self.canvas.itemconfig(self._meter_fill, fill=color)
        except tk.TclError:
            pass

    def _tick(self):
        """Loop: lee peak para el meter y cada ~500ms chequea cambios de mute."""
        # Peak del meter (via IAudioMeterInformation, sin abrir stream)
        try:
            peak = float(self.meter.GetPeakValue())
        except Exception:
            try:
                self.meter = get_mic_meter()
                peak = float(self.meter.GetPeakValue())
            except Exception:
                peak = 0.0
        # Si esta muteado, forzamos barra en 0 (aunque peak podria venir != 0 en shared mode).
        # Aplicamos multiplicador de sensibilidad: voz normal ~0.1 raw -> 0.5 en meter.
        display = 0.0 if self.muted else min(peak * self.METER_SENSITIVITY, 1.0)
        self._set_meter_level(display)

        # Cada N ticks, sincronizar estado de mute con el real del mic
        self._tick_count += 1
        if self._tick_count >= self.MUTE_CHECK_EVERY:
            self._tick_count = 0
            try:
                real_muted = bool(self.volume.GetMute())
            except Exception:
                try:
                    self.volume = get_mic_volume()
                    real_muted = bool(self.volume.GetMute())
                except Exception:
                    real_muted = self.muted
            if real_muted != self.muted:
                self.muted = real_muted
                self._draw()

        try:
            self.root.after(self.TICK_MS, self._tick)
        except tk.TclError:
            return

    def _on_click(self, event):
        try:
            current = self.volume.GetMute()
            self.volume.SetMute(not current, None)
            self.muted = not current
        except Exception:
            self.volume = get_mic_volume()
            current = self.volume.GetMute()
            self.volume.SetMute(not current, None)
            self.muted = not current
        self._draw()

    def _start_drag(self, event):
        self._drag_data = {"x": event.x}

    def _do_drag(self, event):
        dx = event.x - self._drag_data["x"]
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y()
        self.root.geometry(f"+{x}+{y}")

    def _keep_visible(self):
        """Re-eleva la ventana periódicamente para que no quede tapada."""
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
        except tk.TclError:
            return
        self.root.after(5000, self._keep_visible)

    def run(self):
        self._keep_visible()
        self.root.mainloop()


if __name__ == "__main__":
    try:
        app = MicToggle()
        print(f"Mic: {'MUTEADO' if app.muted else 'ACTIVO'}")
        print("Click izquierdo = toggle | Click derecho + arrastrar = mover")
        app.run()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        input("Presiona Enter para cerrar...")
