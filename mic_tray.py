"""
mic_tray.py — Toggle de micrófono en la barra de tareas de Windows.
Requiere: pip install pycaw
"""

import ctypes
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, IAudioMeterInformation

# ── Hotkey global: Ctrl+Shift+F12 ──────────────────────────────
MOD_CTRL = 0x0002
MOD_SHIFT = 0x0004
VK_F12 = 0x7B
HOTKEY_ID = 1

# ── Mutex: impedir múltiples instancias ──────────────────────────
_mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Global\\MicToggleMutex")
if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    ctypes.windll.user32.MessageBoxW(
        0, "MicToggle ya está corriendo.", "MicToggle", 0x40
    )
    sys.exit(0)


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
    COLOR_ACTIVE = "#30d158"         # verde estilo iOS
    COLOR_MUTED = "#ff453a"          # rojo estilo iOS
    COLOR_METER_OFF = "#2a2a2a"      # segmento apagado (casi invisible)
    ICON_SIZE = 32
    PADDING = 4
    METER_WIDTH = 7
    METER_GAP = 6
    METER_SEGMENTS = 8
    METER_SEG_HEIGHT = 3
    METER_SEG_GAP = 1
    # Sensibilidad del meter: multiplicador sobre el peak (voz normal RMS~0.1)
    METER_SENSITIVITY = 5.0

    # Cadencia del tick: 40 ms visual, chequeo de mute cada 12 ticks (~500 ms)
    TICK_MS = 40
    MUTE_CHECK_EVERY = 12

    # Gradiente de colores por segmento (abajo -> arriba): 3 verde, 3 amarillo, 2 rojo
    METER_SEG_COLORS = [
        "#30d158", "#30d158", "#30d158",
        "#ffd60a", "#ffd60a", "#ffd60a",
        "#ff9f0a", "#ff453a",
    ]

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
        cx = icon_w // 2
        cy = h // 2
        r = self.ICON_SIZE // 2

        # Halo sutil (circulo un poco mas grande con color desaturado) para dar profundidad
        self.canvas.create_oval(cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1,
                                fill=self._mix(color, self.TASKBAR_BG, 0.7), outline="")
        # Circulo principal
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")

        # Icono microfono (proporciones mas finas)
        # Cuerpo (capsula redondeada)
        self.canvas.create_oval(cx - 4, cy - 11, cx + 4, cy - 3, fill="white", outline="")
        self.canvas.create_rectangle(cx - 4, cy - 7, cx + 4, cy - 1, fill="white", outline="")
        self.canvas.create_oval(cx - 4, cy - 3, cx + 4, cy + 1, fill="white", outline="")
        # Soporte en U
        self.canvas.create_arc(cx - 7, cy - 4, cx + 7, cy + 6, start=200, extent=140,
                               outline="white", width=2, style="arc")
        # Pie y base
        self.canvas.create_line(cx, cy + 6, cx, cy + 9, fill="white", width=2)
        self.canvas.create_line(cx - 4, cy + 9, cx + 4, cy + 9, fill="white", width=2)

        # Tachado si muteado (diagonal fina, sutil)
        if self.muted:
            self.canvas.create_line(cx - r + 5, cy - r + 5, cx + r - 5, cy + r - 5,
                                    fill="white", width=2)

        # VU meter LED segmentado a la derecha del icono
        meter_x0 = icon_w + self.METER_GAP
        meter_x1 = meter_x0 + self.METER_WIDTH
        total_h = self.METER_SEGMENTS * self.METER_SEG_HEIGHT + \
                  (self.METER_SEGMENTS - 1) * self.METER_SEG_GAP
        top_y = cy - total_h // 2

        self._meter_segments = []
        for i in range(self.METER_SEGMENTS):
            # Segmentos se dibujan de arriba hacia abajo en el array,
            # pero el indice 0 es el de ARRIBA (pico), el ultimo es el de ABAJO (base).
            y0 = top_y + i * (self.METER_SEG_HEIGHT + self.METER_SEG_GAP)
            y1 = y0 + self.METER_SEG_HEIGHT
            seg_id = self.canvas.create_rectangle(
                meter_x0, y0, meter_x1, y1,
                fill=self.COLOR_METER_OFF, outline="",
            )
            self._meter_segments.append(seg_id)

    def _mix(self, hex_a, hex_b, t):
        """Interpola linealmente entre dos colores hex. t=0 -> a, t=1 -> b."""
        def to_rgb(h):
            h = h.lstrip("#")
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        a = to_rgb(hex_a)
        b = to_rgb(hex_b)
        mixed = tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))
        return "#{:02x}{:02x}{:02x}".format(*mixed)

    def _set_meter_level(self, level):
        """level ∈ [0,1] -> enciende N segmentos de abajo hacia arriba."""
        level = max(0.0, min(1.0, level))
        n_total = self.METER_SEGMENTS
        n_on = int(round(level * n_total))

        # _meter_segments[0] es el segmento de arriba (pico), [-1] es el de abajo (base).
        # Si n_on = 3, se encienden los 3 de abajo: indices [n_total-1, n_total-2, n_total-3].
        try:
            for i, seg_id in enumerate(self._meter_segments):
                # i=0 arriba. seg_pos = indice desde abajo (0..n_total-1).
                seg_pos = n_total - 1 - i
                if seg_pos < n_on:
                    color = self.METER_SEG_COLORS[seg_pos]
                else:
                    color = self.COLOR_METER_OFF
                self.canvas.itemconfig(seg_id, fill=color)
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
