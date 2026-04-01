"""
mic_tray.py — Toggle de micrófono en la barra de tareas de Windows.
Requiere: pip install pycaw
"""

import ctypes
import tkinter as tk
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume


def get_mic_volume():
    devices = AudioUtilities.GetMicrophone()
    if devices is None:
        raise RuntimeError("No se encontró micrófono por defecto")
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return interface.QueryInterface(IAudioEndpointVolume)


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
    ICON_SIZE = 32
    PADDING = 4

    def __init__(self):
        self.volume = get_mic_volume()
        self.muted = bool(self.volume.GetMute())

        self.root = tk.Tk()
        self.root.title("Mic")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-toolwindow", True)
        self.root.configure(bg=self.TASKBAR_BG)

        # Tamaño del widget = icono + padding
        w = self.ICON_SIZE + self.PADDING * 2
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

        self._draw()

    def _draw(self):
        self.canvas.delete("all")
        color = self.COLOR_MUTED if self.muted else self.COLOR_ACTIVE
        w = self.ICON_SIZE + self.PADDING * 2
        h = int(self.root.geometry().split("x")[1].split("+")[0])
        # Centrar el círculo verticalmente
        cx = w // 2
        cy = h // 2
        r = self.ICON_SIZE // 2
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")
        # Micrófono
        self.canvas.create_rectangle(cx - 4, cy - 10, cx + 4, cy - 1, fill="white", outline="")
        self.canvas.create_arc(cx - 7, cy - 6, cx + 7, cy + 4, start=180, extent=180,
                               outline="white", width=2, style="arc")
        self.canvas.create_line(cx, cy + 4, cx, cy + 7, fill="white", width=2)
        self.canvas.create_line(cx - 4, cy + 7, cx + 4, cy + 7, fill="white", width=2)
        # Tachado si muteado
        if self.muted:
            self.canvas.create_line(cx - r + 4, cy - r + 4, cx + r - 4, cy + r - 4,
                                    fill="#ffff64", width=2)

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

    def run(self):
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
