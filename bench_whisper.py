"""
Benchmark Whisper small vs medium en CPU.

Graba 20s desde el microfono y transcribe con ambos modelos midiendo:
  - Tiempo de carga del modelo (primera vez baja ~770 MB para medium)
  - Tiempo de transcripcion
  - Factor x realtime (cuanto mas rapido que tiempo real corre)
  - RAM consumida por el modelo
  - Texto resultante (para comparar calidad a ojo)

Uso:
    python bench_whisper.py
"""
import os
import sys
import time
import gc

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

try:
    import psutil
    HAY_PSUTIL = True
except ImportError:
    HAY_PSUTIL = False

SAMPLE_RATE = 16000
DURACION_S = 25
MODEL_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "MicDictado", "models",
)

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


def grabar_audio():
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
    print(">>> Listo.\n")
    return audio.flatten()


def benchmark(nombre, audio):
    print("=" * 64)
    print(f"MODELO: {nombre}")
    print("=" * 64)

    gc.collect()
    ram_antes = ram_proceso_mb()

    t0 = time.time()
    modelo = WhisperModel(
        nombre, device="cpu", compute_type="int8", download_root=MODEL_DIR
    )
    t_carga = time.time() - t0

    ram_cargado = ram_proceso_mb()
    delta_ram = ram_cargado - ram_antes

    print(f"  Tiempo de carga:   {t_carga:>7.1f} s")
    if HAY_PSUTIL:
        print(f"  RAM del modelo:    {delta_ram:>7.0f} MB")

    t0 = time.time()
    segments, info = modelo.transcribe(audio, language="es", beam_size=1)
    texto = " ".join(s.text for s in segments).strip()
    t_trans = time.time() - t0
    factor_rt = DURACION_S / t_trans if t_trans > 0 else 0.0

    print(f"  Transcripcion:     {t_trans:>7.2f} s  ({factor_rt:.1f}x realtime)")
    print(f"\n  Texto resultante:")
    print(f"  >>> {texto}\n")

    del modelo
    gc.collect()

    return {
        "modelo": nombre,
        "t_carga": t_carga,
        "t_trans": t_trans,
        "factor_rt": factor_rt,
        "ram_mb": delta_ram,
        "texto": texto,
    }


def main():
    print("=" * 64)
    print("BENCHMARK Whisper small vs medium (CPU, int8)")
    print("=" * 64)
    print(f"Cache de modelos: {MODEL_DIR}")
    if not HAY_PSUTIL:
        print("(psutil no instalado: no se va a medir RAM. Opcional: pip install psutil)")
    print()

    audio = grabar_audio()

    resultados = []
    for nombre in ["small", "medium"]:
        try:
            resultados.append(benchmark(nombre, audio))
        except Exception as e:
            print(f"ERROR con {nombre}: {e}\n")

    print("=" * 64)
    print("RESUMEN COMPARATIVO")
    print("=" * 64)
    header = f"{'Modelo':<10}{'Carga (s)':<12}{'Trans (s)':<12}{'xRT':<8}"
    if HAY_PSUTIL:
        header += f"{'RAM (MB)':<10}"
    print(header)
    print("-" * len(header))
    for r in resultados:
        row = (
            f"{r['modelo']:<10}"
            f"{r['t_carga']:<12.1f}"
            f"{r['t_trans']:<12.2f}"
            f"{r['factor_rt']:<8.1f}"
        )
        if HAY_PSUTIL:
            row += f"{r['ram_mb']:<10.0f}"
        print(row)

    print()
    print("Pistas para leer el resultado:")
    print(" - xRT > 1.0  -> el modelo transcribe mas rapido que la duracion del audio.")
    print(" - Compara cada texto contra el TEXTO DE REFERENCIA de arriba.")
    print("   Los puntos donde small suele fallar y medium acertar:")
    print("     * Nombres propios: Martin Belli, MicDictado, Santex, Llama")
    print("     * Versiones / numeros: int8, RTX 2060, 4.2, 1.8, 3.2")
    print("     * Anglicismos tecnicos: tool calling, Slack, Gmail")


if __name__ == "__main__":
    main()
