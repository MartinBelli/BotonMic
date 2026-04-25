"""
tray.py — Icono en la bandeja del sistema + ventana de Configuracion.

Icon en hilo daemon (pystray). Click izquierdo o "Toggle dictado" disparan el
mismo flujo que el hotkey. "Configuracion..." abre una ventana tkinter Toplevel
con sliders/toggles para todos los settings persistentes.

Los callbacks del tray corren en el hilo de pystray, asi que cualquier accion
que toque tkinter se posterga via overlay.root.after(0, ...).
"""

import threading
import tkinter as tk
from tkinter import ttk

import pystray
from PIL import Image, ImageDraw

from settings import settings, DEFAULTS


def _make_icon_image():
    """Genera un PNG 64x64 con un circulo rojo. Sin assets externos."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Disco rojo (mismo color que COLOR_REC del overlay)
    d.ellipse((6, 6, 58, 58), fill=(220, 50, 50, 255))
    # Punto interior blanco para que se vea aunque la barra de tareas sea oscura/clara
    d.ellipse((26, 26, 38, 38), fill=(255, 255, 255, 255))
    return img


class TrayIcon:
    def __init__(self, app):
        self.app = app  # MicDictado
        self._settings_window = None
        self._icon = pystray.Icon(
            "MicDictado",
            _make_icon_image(),
            "MicDictado",
            menu=pystray.Menu(
                pystray.MenuItem("Toggle dictado", self._on_toggle, default=True),
                pystray.MenuItem("Configuración…", self._on_open_settings),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Salir", self._on_quit),
            ),
        )

    def _sched(self, fn):
        """Postea fn al hilo de Tk para evitar tocar widgets desde el hilo de pystray."""
        self.app.overlay.root.after(0, fn)

    def _on_toggle(self, icon=None, item=None):
        self._sched(self.app._toggle)

    def _on_open_settings(self, icon=None, item=None):
        self._sched(self._open_settings_window)

    def _on_quit(self, icon=None, item=None):
        self._icon.stop()
        # Cerrar Tk desde su propio hilo
        self._sched(self.app.overlay.root.destroy)

    def _open_settings_window(self):
        """Abre (o levanta) la ventana de Configuracion. Modal-ish (siempre topmost)."""
        if self._settings_window is not None and self._settings_window.winfo_exists():
            self._settings_window.deiconify()
            self._settings_window.lift()
            self._settings_window.focus_force()
            return
        self._settings_window = SettingsWindow(self.app)

    def start(self):
        threading.Thread(target=self._icon.run, daemon=True).start()


class SettingsWindow(tk.Toplevel):
    """Ventana de configuracion. Sliders aplican en vivo; el modelo recarga al cerrar
    si cambio."""

    PADX = 12
    PADY = 6

    def __init__(self, app):
        super().__init__(app.overlay.root)
        self.app = app
        self.title("MicDictado — Configuración")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        # Centrar relativo a la pantalla
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = self.winfo_width()
        h = self.winfo_height()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _build(self):
        pad = {"padx": self.PADX, "pady": self.PADY}

        # ── VU meter ──
        f_vu = ttk.LabelFrame(self, text="VU meter")
        f_vu.pack(fill="x", **pad)
        ttk.Label(f_vu, text="Sensibilidad").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.var_vu = tk.DoubleVar(value=float(settings.vu_gain))
        self.lbl_vu = ttk.Label(f_vu, text=f"{self.var_vu.get():.1f}", width=4)
        self.lbl_vu.grid(row=0, column=2, padx=8)
        scale_vu = ttk.Scale(
            f_vu, from_=1.0, to=15.0, orient="horizontal",
            variable=self.var_vu, command=self._on_vu_change,
            length=240,
        )
        scale_vu.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        f_vu.columnconfigure(1, weight=1)

        # ── Ducking ──
        f_duck = ttk.LabelFrame(self, text="Ducking de música")
        f_duck.pack(fill="x", **pad)
        self.var_duck_on = tk.BooleanVar(value=bool(settings.ducking_enabled))
        ttk.Checkbutton(
            f_duck, text="Bajar volumen de música/video al grabar",
            variable=self.var_duck_on, command=self._on_duck_toggle,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        ttk.Label(f_duck, text="Volumen objetivo:").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.var_duck_pct = tk.IntVar(value=int(settings.ducking_volume_pct))
        self.lbl_duck = ttk.Label(f_duck, text=f"{self.var_duck_pct.get()}%", width=5)
        self.lbl_duck.grid(row=1, column=2, padx=8)
        ttk.Scale(
            f_duck, from_=0, to=100, orient="horizontal",
            variable=self.var_duck_pct, command=self._on_duck_pct_change,
            length=200,
        ).grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Label(f_duck, text="Excluir procesos (separar con coma):").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 2)
        )
        self.var_duck_excl = tk.StringVar(value=", ".join(settings.ducking_exclude))
        entry_excl = ttk.Entry(f_duck, textvariable=self.var_duck_excl)
        entry_excl.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=2)
        f_duck.columnconfigure(1, weight=1)

        # ── Transcripcion ──
        f_trans = ttk.LabelFrame(self, text="Transcripción")
        f_trans.pack(fill="x", **pad)
        ttk.Label(f_trans, text="Modelo:").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.var_model = tk.StringVar(value=settings.model_name)
        models = [("tiny", "Tiny (rápido, menos preciso)"),
                  ("base", "Base"),
                  ("small", "Small (recomendado)"),
                  ("medium", "Medium (lento, más preciso)")]
        for i, (val, lbl) in enumerate(models):
            ttk.Radiobutton(
                f_trans, text=lbl, variable=self.var_model, value=val,
            ).grid(row=1 + i, column=0, columnspan=2, sticky="w", padx=20, pady=1)

        self.var_vad = tk.BooleanVar(value=bool(settings.vad_filter))
        ttk.Checkbutton(
            f_trans, text="VAD filter (recorta silencios, recomendado)",
            variable=self.var_vad, command=self._on_vad_change,
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 2))

        ttk.Label(f_trans, text="Beam size (1 = más rápido, 5 = más preciso):").grid(
            row=6, column=0, sticky="w", padx=8, pady=4
        )
        self.var_beam = tk.IntVar(value=int(settings.beam_size))
        ttk.Spinbox(
            f_trans, from_=1, to=5, width=4, textvariable=self.var_beam,
            command=self._on_beam_change,
        ).grid(row=6, column=1, sticky="w", pady=4)

        # ── Hotkeys (referencia) ──
        f_keys = ttk.LabelFrame(self, text="Hotkeys")
        f_keys.pack(fill="x", **pad)
        hotkeys = [
            ("Ctrl+Shift+F11", "Grabar / parar (auto-pega al terminar)"),
            ("Ctrl+Shift+Space", "Re-pegar el último texto donde esté el cursor"),
            ("Ctrl+Shift+F9", "Abrir esta ventana de Configuración"),
        ]
        for i, (combo, desc) in enumerate(hotkeys):
            ttk.Label(f_keys, text=combo, font=("Segoe UI", 9, "bold")).grid(
                row=i, column=0, sticky="w", padx=8, pady=2
            )
            ttk.Label(f_keys, text=desc).grid(row=i, column=1, sticky="w", padx=8, pady=2)

        # ── Botones ──
        f_btn = ttk.Frame(self)
        f_btn.pack(fill="x", **pad)
        ttk.Button(f_btn, text="Cerrar", command=self._on_close).pack(side="right", padx=4)
        ttk.Button(f_btn, text="Aplicar", command=self._apply_all).pack(side="right", padx=4)

    # ── Handlers en vivo (no requieren reload) ──

    def _on_vu_change(self, _val=None):
        v = float(self.var_vu.get())
        self.lbl_vu.config(text=f"{v:.1f}")
        settings.update(vu_gain=v)

    def _on_duck_toggle(self):
        settings.update(ducking_enabled=bool(self.var_duck_on.get()))

    def _on_duck_pct_change(self, _val=None):
        v = int(self.var_duck_pct.get())
        self.lbl_duck.config(text=f"{v}%")
        settings.update(ducking_volume_pct=v)

    def _on_vad_change(self):
        settings.update(vad_filter=bool(self.var_vad.get()))

    def _on_beam_change(self):
        try:
            settings.update(beam_size=int(self.var_beam.get()))
        except (ValueError, tk.TclError):
            pass

    # ── Aplicar todo / cerrar ──

    def _apply_all(self):
        # Las variables que aplican en vivo ya estan persistidas; aca persistimos las que no:
        # excluidos del ducking + modelo. El modelo gatilla reload.
        excl_raw = self.var_duck_excl.get().strip()
        excl = [p.strip() for p in excl_raw.split(",") if p.strip()] if excl_raw else []
        settings.update(
            ducking_exclude=excl or list(DEFAULTS["ducking_exclude"]),
            model_name=self.var_model.get(),
        )
        # Forzar persistencia y reload de modelo si cambio
        self.app.reload_model_if_needed()

    def _on_close(self):
        # Persistir todo (por si quedaron cambios sin "Aplicar")
        self._apply_all()
        try:
            self.destroy()
        except tk.TclError:
            pass
