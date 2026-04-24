"""
mic_tray.py — Toggle de micrófono en la barra de tareas de Windows.

Widget con disco gris, icono de microfono neutro y halo glow en arco
superior (9 a 3) que se ilumina verde (activo) o rojo (muteado). Cuando
hablas, el halo verde crece en grosor y satura con el nivel de voz.

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
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

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


def get_taskbar_rect():
    """Obtiene posición y tamaño de la barra de tareas."""
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


class _AudioMonitor:
    """Stream permanente de sounddevice que publica el nivel RMS actual del mic.

    Necesario porque IAudioMeterInformation no reporta peak en el Focusrite y
    otros drivers USB si no hay un cliente activo grabando. Con un InputStream
    propio siempre abierto, tenemos RMS confiable en todo momento.

    level: float [0,1], atomico. Leelo desde el hilo de Tk sin lock.
    """

    SENSITIVITY = 4.0  # multiplicador sobre RMS raw (voz normal ~0.05-0.15)
    WATCHDOG_INTERVAL_S = 2.0
    BACKOFF_MAX_S = 10.0

    def __init__(self):
        self.level = 0.0
        self._stream = None
        self._current_device = None
        self._shutdown = False
        self._backoff = 1.0

        self._open()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()
        atexit.register(self.close)

    def _callback(self, indata, frames, time_info, status):
        # RMS del chunk normalizado [0,1], multiplicado por sensibilidad.
        try:
            rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            self.level = min(rms * self.SENSITIVITY, 1.0)
        except Exception:
            self.level = 0.0

    def _open(self):
        """Abre el InputStream del device default. Si falla deja _stream=None."""
        try:
            device = sd.default.device[0]
            if device is None or device < 0:
                # sd.default puede ser (-1,-1) si no hay default. Usar hostapi.
                device = sd.query_hostapis()[sd.default.hostapi]["default_input_device"]
            stream = sd.InputStream(
                device=device,
                samplerate=None,   # nativo del device, evita "Invalid sample rate"
                channels=1,
                dtype="float32",
                blocksize=0,
                latency="low",
                callback=self._callback,
            )
            stream.start()
            self._stream = stream
            self._current_device = device
        except Exception as e:
            print(f"[AudioMonitor] no pude abrir stream: {e}")
            self._stream = None

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _watchdog_loop(self):
        """Detecta stream muerto (sleep, desconexion) o cambio de default device."""
        while not self._shutdown:
            time.sleep(self.WATCHDOG_INTERVAL_S)
            if self._shutdown:
                return
            try:
                needs_reopen = False
                if self._stream is None:
                    needs_reopen = True
                else:
                    try:
                        if not self._stream.active:
                            needs_reopen = True
                    except Exception:
                        needs_reopen = True
                    # Comparamos default actual vs el que abrimos
                    try:
                        new_default = sd.default.device[0]
                        if new_default is not None and new_default != self._current_device:
                            needs_reopen = True
                    except Exception:
                        pass

                if needs_reopen:
                    self.level = 0.0
                    self._close_stream()
                    self._open()
                    if self._stream is not None:
                        self._backoff = 1.0
                    else:
                        # Backoff progresivo si sigue fallando (mic fisicamente desconectado)
                        time.sleep(self._backoff)
                        self._backoff = min(self._backoff * 2.0, self.BACKOFF_MAX_S)
            except Exception as e:
                print(f"[AudioMonitor] watchdog error: {e}")

    def close(self):
        self._shutdown = True
        self._close_stream()


class MicToggle:
    TASKBAR_BG = "#1f1f1f"
    # Disco e icono
    DISC_COLOR = "#2a2a2a"
    MIC_COLOR = "#9a9a9a"
    # Halo colors
    HALO_GREEN_LOW = "#3fa861"    # verde claro en reposo
    HALO_GREEN_HIGH = "#0a6f2a"   # verde oscuro saturado hablando
    HALO_RED = "#c84545"          # rojo mute constante

    ICON_SIZE = 32
    HALO_MARGIN = 10  # margen alrededor del disco para los arcos del halo

    # Cadencia del tick: 40 ms visual, chequeo de mute cada 12 ticks (~500 ms)
    TICK_MS = 40
    MUTE_CHECK_EVERY = 12

    # Radios adicionales para los 4 arcos del halo (sobre r del disco)
    HALO_OFFSETS = (2, 4, 6, 8)
    # Fade base de los 3 exteriores (0 = no fade, 1 = puro BG). Interior tiene fade 0.
    HALO_OUTER_FADE = (0.3, 0.55, 0.8)

    def __init__(self):
        self.volume = get_mic_volume()
        self.muted = bool(self.volume.GetMute())
        self.monitor = _AudioMonitor()

        self.root = tk.Tk()
        self.root.title("Mic")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-toolwindow", True)
        self.root.configure(bg=self.TASKBAR_BG)

        # Widget cuadrado: icono + margen para el halo de ambos lados
        w = self.ICON_SIZE + self.HALO_MARGIN * 2
        taskbar = get_taskbar_rect()
        taskbar_h = taskbar.bottom - taskbar.top

        x = taskbar.right - w - 200  # a la izquierda del systray
        y = taskbar.top
        h = taskbar_h

        # Guardamos dimensiones como atributos: no leer self.root.geometry() en _draw
        # porque Tk puede no haber aplicado la geometry aun al primer dibujo,
        # devolviendo "1x1+0+0" y rompiendo el layout hasta el primer redraw.
        self._widget_w = w
        self._widget_h = h

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

        self._halo_arcs = []  # 4 arc items, orden: interior -> exterior
        self._tick_count = 0
        self._draw()
        self._tick()

        # Hotkey global Ctrl+Shift+F12
        self._hotkey_thread = threading.Thread(target=self._listen_hotkey, daemon=True)
        self._hotkey_thread.start()

    def _listen_hotkey(self):
        ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, MOD_CTRL | MOD_SHIFT, VK_F12)
        msg = wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            if msg.message == 0x0312:  # WM_HOTKEY
                self.root.after(0, self._on_click, None)

    @staticmethod
    def _mix(hex_a, hex_b, t):
        """Lerp entre dos colores hex en espacio RGB. t=0 -> a, t=1 -> b."""
        def to_rgb(h):
            h = h.lstrip("#")
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        a = to_rgb(hex_a)
        b = to_rgb(hex_b)
        t = max(0.0, min(1.0, t))
        r = int(a[0] * (1 - t) + b[0] * t)
        g = int(a[1] * (1 - t) + b[1] * t)
        bl = int(a[2] * (1 - t) + b[2] * t)
        return f"#{r:02x}{g:02x}{bl:02x}"

    def _draw(self):
        self.canvas.delete("all")
        w = self._widget_w
        h = self._widget_h
        cx = w // 2
        cy = h // 2
        r = self.ICON_SIZE // 2

        # Halo: 4 arcos concentricos en la mitad superior (9 a 3 en reloj).
        # Tk create_arc con start=0, extent=180 traza de angulo 0° (3 en reloj)
        # hacia 180° (9 en reloj) pasando por arriba.
        self._halo_arcs = []
        for extra in self.HALO_OFFSETS:
            arc_id = self.canvas.create_arc(
                cx - (r + extra), cy - (r + extra),
                cx + (r + extra), cy + (r + extra),
                start=0, extent=180, style="arc",
                outline=self.TASKBAR_BG, width=2,
            )
            self._halo_arcs.append(arc_id)

        # Disco base gris
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                fill=self.DISC_COLOR, outline="")
        # Microfono en gris claro
        mc = self.MIC_COLOR
        self.canvas.create_rectangle(cx - 4, cy - 10, cx + 4, cy - 1, fill=mc, outline="")
        self.canvas.create_arc(cx - 7, cy - 6, cx + 7, cy + 4, start=180, extent=180,
                               outline=mc, width=2, style="arc")
        self.canvas.create_line(cx, cy + 4, cx, cy + 7, fill=mc, width=2)
        self.canvas.create_line(cx - 4, cy + 7, cx + 4, cy + 7, fill=mc, width=2)

        # Pintar el halo inicial
        self._set_halo(0.0)

    def _set_halo(self, level):
        """level ∈ [0,1] -> actualiza color y grosor del halo segun estado+nivel."""
        if not self._halo_arcs:
            return
        level = max(0.0, min(1.0, level))

        if self.muted:
            core_color = self.HALO_RED
            effective_level = 0.0  # rojo constante, no responde al nivel
        else:
            # Verde claro (silencio) -> verde oscuro saturado (hablando)
            core_color = self._mix(self.HALO_GREEN_LOW, self.HALO_GREEN_HIGH, level)
            effective_level = level

        # Arco interior: width variable con el nivel (2 -> 6)
        try:
            interior_width = 2 + int(effective_level * 4)
            self.canvas.itemconfig(self._halo_arcs[0],
                                   outline=core_color, width=interior_width)
            # Exteriores: fade progresivo hacia el BG. A mayor level, menos fade
            # -> halo parece "crecer" visualmente aunque los radios sean fijos.
            for i, fade_base in enumerate(self.HALO_OUTER_FADE):
                # fade: en level=1 usa fade_base; en level=0 fade = fade_base + 0.2 (mas BG)
                fade = min(1.0, fade_base + (1.0 - effective_level) * 0.2)
                color = self._mix(core_color, self.TASKBAR_BG, fade)
                self.canvas.itemconfig(self._halo_arcs[i + 1],
                                       outline=color, width=2)
        except tk.TclError:
            pass

    def _tick(self):
        # Nivel actual desde el monitor de audio permanente.
        # Aplicamos compresor suave (sqrt) para expandir rango bajo y comprimir saturacion.
        raw = self.monitor.level
        display = 0.0 if self.muted else min(1.0, raw) ** 0.5
        self._set_halo(display)

        # Cada N ticks, sincronizar estado de mute con el real del mic.
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
                # Si cambio el estado, forzamos redraw para que el proximo _set_halo
                # use el color correcto. Los arcos ya existen, solo el color cambia.
                self._set_halo(display)

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
        # Forzar refresh inmediato del halo
        self._set_halo(0.0 if self.muted else min(1.0, self.monitor.level) ** 0.5)

    def _start_drag(self, event):
        self._drag_data = {"x": event.x}

    def _do_drag(self, event):
        dx = event.x - self._drag_data["x"]
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y()
        self.root.geometry(f"+{x}+{y}")

    def _keep_visible(self):
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
