"""
mic_tray.py — Toggle de micrófono en la bandeja del sistema de Windows.
Requiere: pip install pystray pillow pycaw
"""

import pystray
from PIL import Image, ImageDraw
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume


def get_mic_volume():
    """Obtiene la interfaz IAudioEndpointVolume del micrófono por defecto."""
    devices = AudioUtilities.GetMicrophone()
    if devices is None:
        raise RuntimeError("No se encontró micrófono por defecto")
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return interface.QueryInterface(IAudioEndpointVolume)


def is_muted(volume):
    """Retorna True si el mic está muteado."""
    return bool(volume.GetMute())


def toggle_mute(volume):
    """Cambia el estado de mute del mic. Retorna el nuevo estado."""
    current = volume.GetMute()
    volume.SetMute(not current, None)
    return not current


def make_icon(muted):
    """Crea el ícono: verde = activo, rojo con línea = muteado."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    color = (220, 50, 50, 255) if muted else (40, 190, 100, 255)
    draw.ellipse([4, 4, 60, 60], fill=color)

    # Micrófono
    draw.rectangle([26, 14, 38, 36], fill=(255, 255, 255, 220), outline=(255, 255, 255))
    draw.arc([20, 28, 44, 48], 0, 180, fill=(255, 255, 255, 220), width=3)
    draw.line([32, 48, 32, 54], fill=(255, 255, 255, 220), width=3)
    draw.line([24, 54, 40, 54], fill=(255, 255, 255, 220), width=3)

    if muted:
        draw.line([14, 14, 50, 50], fill=(255, 255, 100, 255), width=4)

    return img


class MicTray:
    def __init__(self):
        self.volume = get_mic_volume()
        self.muted = is_muted(self.volume)
        self.icon = None

    def toggle(self, icon=None, item=None):
        self.muted = toggle_mute(self.volume)
        self._update()

    def _update(self):
        if self.icon:
            self.icon.icon = make_icon(self.muted)
            estado = "Muteado" if self.muted else "Activo"
            self.icon.title = f"Mic — {estado}"
            self.icon.menu = self._menu()

    def _menu(self):
        estado = "Mic muteado" if self.muted else "Mic activo"
        accion = "Activar mic" if self.muted else "Mutear mic"
        return pystray.Menu(
            pystray.MenuItem(estado, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(accion, self.toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", lambda icon, item: icon.stop()),
        )

    def run(self):
        self.icon = pystray.Icon(
            "mic_tray",
            make_icon(self.muted),
            f"Mic — {'Muteado' if self.muted else 'Activo'}",
            menu=self._menu(),
        )
        self.icon.default_action = self.toggle
        self.icon.run()


if __name__ == "__main__":
    try:
        print("Iniciando MicTray...")
        app = MicTray()
        print(f"Mic detectado. Estado: {'MUTEADO' if app.muted else 'ACTIVO'}")
        print("Icono en la bandeja. Click izquierdo = toggle, derecho = menu.")
        app.run()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        input("Presiona Enter para cerrar...")
