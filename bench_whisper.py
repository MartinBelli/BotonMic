"""
Benchmark Whisper en distintos device / compute_type / modelos.

Graba unos segundos desde el microfono y transcribe con cada combinacion
del set de configuraciones definido (CONFIGS) midiendo:
  - Tiempo de carga del modelo (primera vez baja ~470 MB para small)
  - Tiempo de transcripcion (warm: con kernels ya calentados)
  - Factor x realtime (cuanto mas rapido que tiempo real corre)
  - RAM proceso + (si CUDA) VRAM al cargar
  - Texto resultante (para comparar calidad a ojo)

Hace 2 corridas de transcripcion por config: la primera incluye el costo de
"calentar" los kernels CUDA / cache CTranslate2, que distorsiona la lectura.
Reportamos la SEGUNDA (warm) como representativa del uso real ya en marcha.

Al final guarda el resumen + los textos transcriptos en
bench_results.txt en el cwd, para que no se pierdan si el output
de la consola se trunca al copiar/pegar.

Uso:
    python bench_whisper.py
"""
import datetime
import gc
import os
import sys
import time

import numpy as np
import sounddevice as sd

# Registrar DLLs CUDA antes de importar faster_whisper (idem mic_dictado.py).
from cuda_dlls import setup_cuda_dlls
setup_cuda_dlls()

from faster_whisper import WhisperModel

try:
    import psutil
    HAY_PSUTIL = True
except ImportError:
    HAY_PSUTIL = False

try:
    import ctranslate2
    HAY_CT2 = True
    CUDA_DEVICES = ctranslate2.get_cuda_device_count()
except Exception:
    HAY_CT2 = False
    CUDA_DEVICES = 0

SAMPLE_RATE = 16000
DURACION_S = 20
RESULT_FILE = "bench_results.txt"
AUDIO_FILE = "bench_audio.wav"
MODEL_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "MicDictado", "models",
)

# Configuraciones a probar. Si CUDA no esta disponible, las CUDA se saltean
# automaticamente con un mensaje. Para Fase 1 nos enfocamos en small + small CUDA.
# La Fase 2 va a agregar large-v3-turbo a esta lista.
CONFIGS = [
    {"name": "small / cpu int8",        "model": "small",  "device": "cpu",  "compute_type": "int8"},
    {"name": "small / cuda float16",    "model": "small",  "device": "cuda", "compute_type": "float16"},
]

# Texto de prueba pensado para mostrar diferencias entre small y medium:
#  - Nombres propios (Martin Belli, MicDictado, Santex, OpenAI, Whisper, Llama)
#  - Numeros y versiones (14 de marzo, int8, 4.2, 1.8, RTX 2060, 3.2)
#  - Anglicismos tecnicos (tool calling, sprint, Slack, Gmail)
#  - Acentos cerrados y tildes (reunion, latencia, integracion, proximo)
TEXTO_REFERENCIA = (
    "En la reunion del 14 de marzo, Martin Belli presento MicDictado a Santex Group. "
    "Decidimos usar Whisper de OpenAI corriendo en local con cuantizacion int8. "
    "La latencia bajo de 4.2 a 1.8 segundos en una RTX 2060. "
    "El proximo paso es integrar Llama 3.2 con tool calling, "
    "para mandar mensajes por Slack y Gmail directo desde el dictado por voz."
)


def ram_proceso_mb():
    if not HAY_PSUTIL:
        return 0.0
    return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024


def _diagnostico_audio(audio):
    """Calcula metricas para detectar si se capturo voz o silencio."""
    if audio.size == 0:
        return {"rms": 0.0, "peak": 0.0, "pct_silencio": 100.0}
    rms = float(np.sqrt(np.mean(audio**2)))
    peak = float(np.max(np.abs(audio)))
    # umbral de silencio: muestras con |x| < 0.001 (-60 dBFS aprox)
    pct_silencio = float(np.mean(np.abs(audio) < 0.001) * 100)
    return {"rms": rms, "peak": peak, "pct_silencio": pct_silencio}


def _guardar_wav(audio, path):
    """Guarda el audio float32 [-1,1] como WAV PCM 16-bit, sin dependencias extra."""
    import wave
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())


def grabar_audio():
    # Mostrar el dispositivo de input que sounddevice va a usar.
    # Si esto sale raro, ya sabemos por que el audio salio vacio.
    try:
        idx_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        info = sd.query_devices(idx_in, "input")
        print(f">>> Dispositivo de entrada: [{idx_in}] {info['name']}")
        print(f"    canales: {info['max_input_channels']}, sample_rate default: {info['default_samplerate']:.0f} Hz")
    except Exception as e:
        print(f">>> No pude detectar el dispositivo de entrada: {e}")

    print("\n" + "=" * 64)
    print("TEXTO PARA LEER EN VOZ ALTA")
    print("=" * 64)
    print()
    print(TEXTO_REFERENCIA)
    print()
    print("=" * 64)
    print(f"Tenes {DURACION_S} segundos. Lee a ritmo natural, sin apurarte.")
    print("Si te sobra tiempo al final, mejor: dejamos margen.")
    print("Tilde y acento donde corresponda (reunion -> reunion con tilde, etc).")
    print("IMPORTANTE: cerra la app MicDictado antes de correr esto, sino")
    print("el ducking puede silenciar el mic y grabamos puro silencio.")
    print("=" * 64)
    input("\n>>> Apreta ENTER cuando estes listo... ")
    print(">>> GRABANDO...")
    audio = sd.rec(
        int(DURACION_S * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    audio = audio.flatten()
    print(">>> Listo.")

    # Diagnostico: ¿realmente capto algo?
    diag = _diagnostico_audio(audio)
    print(f">>> Audio capturado: RMS={diag['rms']:.4f}  Peak={diag['peak']:.3f}  Silencio={diag['pct_silencio']:.1f}%")
    if diag["peak"] < 0.01:
        print(">>> ALERTA: el audio parece estar EN SILENCIO. Probable mic muteado o")
        print(">>> dispositivo de entrada equivocado. Las transcripciones van a salir vacias.")
    elif diag["pct_silencio"] > 90:
        print(">>> ALERTA: el audio es mayormente silencio (>90%). Capaz hablaste muy bajo")
        print(">>> o el mic esta lejos.")

    # Guardar WAV para poder escucharlo manualmente si hace falta.
    try:
        _guardar_wav(audio, AUDIO_FILE)
        print(f">>> WAV guardado en: {os.path.abspath(AUDIO_FILE)}")
    except OSError as e:
        print(f">>> No pude guardar el WAV: {e}")

    print()
    return audio


def benchmark(cfg, audio):
    nombre = cfg["name"]
    print("=" * 64)
    print(f"CONFIG: {nombre}")
    print("=" * 64)

    if cfg["device"] == "cuda" and CUDA_DEVICES == 0:
        print("  CUDA pedido pero no hay GPU detectada por ctranslate2; salteando.\n")
        return None

    gc.collect()
    ram_antes = ram_proceso_mb()

    t0 = time.time()
    try:
        modelo = WhisperModel(
            cfg["model"],
            device=cfg["device"],
            compute_type=cfg["compute_type"],
            download_root=MODEL_DIR,
        )
    except Exception as e:
        print(f"  ERROR al cargar: {e}\n")
        return None
    t_carga = time.time() - t0

    ram_cargado = ram_proceso_mb()
    delta_ram = ram_cargado - ram_antes

    print(f"  Tiempo de carga:    {t_carga:>7.1f} s")
    if HAY_PSUTIL:
        print(f"  RAM del modelo:     {delta_ram:>7.0f} MB")

    # Primera corrida (cold): incluye compilacion de kernels CUDA / cache CT2.
    # Reportamos pero no usamos para comparar; sirve para ver el "primer hit".
    t0 = time.time()
    segments, _ = modelo.transcribe(audio, language="es", beam_size=1)
    texto_cold = " ".join(s.text for s in segments).strip()
    t_cold = time.time() - t0
    rt_cold = DURACION_S / t_cold if t_cold > 0 else 0.0
    print(f"  Transcripcion cold: {t_cold:>7.2f} s  ({rt_cold:.1f}x realtime)")

    # Segunda corrida (warm): representativa del uso real ya en marcha.
    t0 = time.time()
    segments, _ = modelo.transcribe(audio, language="es", beam_size=1)
    texto_warm = " ".join(s.text for s in segments).strip()
    t_warm = time.time() - t0
    rt_warm = DURACION_S / t_warm if t_warm > 0 else 0.0
    print(f"  Transcripcion warm: {t_warm:>7.2f} s  ({rt_warm:.1f}x realtime)")

    print(f"\n  Texto resultante (warm):")
    print(f"  >>> {texto_warm}\n")

    del modelo
    gc.collect()

    return {
        "name": nombre,
        "model": cfg["model"],
        "device": cfg["device"],
        "compute_type": cfg["compute_type"],
        "t_carga": t_carga,
        "t_cold": t_cold,
        "t_warm": t_warm,
        "rt_warm": rt_warm,
        "ram_mb": delta_ram,
        "texto": texto_warm,
    }


def main():
    print("=" * 64)
    print("BENCHMARK Whisper - configuraciones device/compute_type")
    print("=" * 64)
    print(f"Cache de modelos: {MODEL_DIR}")
    print(f"CUDA disponible: {'si (' + str(CUDA_DEVICES) + ' GPU)' if CUDA_DEVICES else 'no'}")
    if not HAY_PSUTIL:
        print("(psutil no instalado: no se va a medir RAM. Opcional: pip install psutil)")
    print()

    audio = grabar_audio()

    resultados = []
    for cfg in CONFIGS:
        try:
            r = benchmark(cfg, audio)
            if r is not None:
                resultados.append(r)
        except Exception as e:
            print(f"ERROR con {cfg['name']}: {e}\n")

    if not resultados:
        print("No hubo resultados. Salgo.")
        return

    # ── Resumen formateado: imprimir a consola Y guardar en archivo ──
    lineas = []
    lineas.append("=" * 64)
    lineas.append("RESUMEN COMPARATIVO (warm = 2da corrida, kernels calientes)")
    lineas.append("=" * 64)
    lineas.append(f"Fecha: {datetime.datetime.now().isoformat(timespec='seconds')}")
    lineas.append(f"Duracion del audio: {DURACION_S} s")
    lineas.append("")
    header = f"{'Config':<24}{'Carga(s)':<10}{'Cold(s)':<10}{'Warm(s)':<10}{'xRT':<7}"
    if HAY_PSUTIL:
        header += f"{'RAM(MB)':<9}"
    lineas.append(header)
    lineas.append("-" * len(header))
    for r in resultados:
        row = (
            f"{r['name']:<24}"
            f"{r['t_carga']:<10.1f}"
            f"{r['t_cold']:<10.2f}"
            f"{r['t_warm']:<10.2f}"
            f"{r['rt_warm']:<7.1f}"
        )
        if HAY_PSUTIL:
            row += f"{r['ram_mb']:<9.0f}"
        lineas.append(row)

    # Speedup vs el primer config (CPU baseline)
    if len(resultados) >= 2:
        baseline = resultados[0]["t_warm"]
        lineas.append("")
        lineas.append("Speedup vs " + resultados[0]["name"] + " (basado en warm):")
        for r in resultados[1:]:
            sp = baseline / r["t_warm"] if r["t_warm"] > 0 else 0.0
            lineas.append(f"  {r['name']:<24} {sp:.2f}x")

    lineas.append("")
    lineas.append("=" * 64)
    lineas.append("TEXTO DE REFERENCIA (lo que tenias que leer)")
    lineas.append("=" * 64)
    lineas.append(TEXTO_REFERENCIA)
    lineas.append("")
    for r in resultados:
        lineas.append("=" * 64)
        lineas.append(f"TEXTO TRANSCRIPTO POR: {r['name']}")
        lineas.append("=" * 64)
        lineas.append(r["texto"] or "(vacio)")
        lineas.append("")

    lineas.append("Pistas para leer el resultado:")
    lineas.append(" - xRT > 1.0 -> el modelo transcribe mas rapido que el audio.")
    lineas.append(" - 'cold' incluye compilacion de kernels CUDA y cache; 'warm' es")
    lineas.append("   representativo del uso ya en marcha tras el primer disparo.")
    lineas.append(" - Comparar cada texto contra el TEXTO DE REFERENCIA: nombres")
    lineas.append("   propios (Martin Belli, MicDictado, Santex, Llama), versiones")
    lineas.append("   (int8, RTX 2060, 4.2, 1.8, 3.2) y anglicismos (tool calling).")

    salida = "\n".join(lineas)
    print(salida)

    try:
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            f.write(salida + "\n")
        print(f"\n>>> Resultados guardados tambien en: {os.path.abspath(RESULT_FILE)}")
    except OSError as e:
        print(f"\n>>> No pude guardar el archivo de resultados: {e}")


if __name__ == "__main__":
    main()
