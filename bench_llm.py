"""
Benchmark Llama 3.2 3B Q4 + Whisper small en CPU.

Mide en una sola corrida:
  - Tiempo de carga del LLM
  - Tokens/seg en generacion
  - RAM con Whisper small + LLM cargados en simultaneo
  - Latencia de 3 prompts representativos del producto:
      1. Reescritura  (Wispr Flow offline)
      2. Tool calling (atajos por voz)
      3. Correccion con glosario (mejorar transcripciones de Whisper)

Guarda los resultados en bench_llm_results.txt para no depender del
copy-paste de la consola.

Pre-requisitos:
  - llama-cpp-python instalado
  - faster-whisper instalado
  - modelo GGUF en %LOCALAPPDATA%\\MicDictado\\llm\\Llama-3.2-3B-Instruct-Q4_K_M.gguf

Uso:
    python bench_llm.py
"""
import datetime
import gc
import os
import time

try:
    import psutil
    HAY_PSUTIL = True
except ImportError:
    HAY_PSUTIL = False

from faster_whisper import WhisperModel
from llama_cpp import Llama


APP_DATA = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "MicDictado",
)
MODEL_DIR_WHISPER = os.path.join(APP_DATA, "models")
MODEL_PATH_LLM = os.path.join(APP_DATA, "llm", "Llama-3.2-3B-Instruct-Q4_K_M.gguf")
RESULT_FILE = "bench_llm_results.txt"


# Prompts pensados para cubrir los 3 casos de uso del producto.
# Cada uno fuerza al modelo a hacer algo distinto:
#   - 1: reescribir manteniendo intencion pero cambiando registro
#   - 2: estructurar (devolver JSON) — clave para los atajos
#   - 3: corregir con glosario — clave para post-procesado de Whisper
PROMPTS = [
    {
        "nombre": "1. Reescritura (email formal)",
        "system": (
            "Sos un asistente que reescribe textos en espanol de forma profesional "
            "y concisa. Devolve SOLO el texto reescrito, sin explicaciones."
        ),
        "user": (
            "Reescribi esto como email formal a un colega: "
            "che pedro, te queria pasar el reporte que me pediste, fijate que "
            "esta en el drive, cualquier cosa avisame. saludos"
        ),
        "max_tokens": 200,
    },
    {
        "nombre": "2. Tool calling (intent + parametros)",
        "system": (
            "Sos un asistente que extrae intent y parametros de comandos por voz "
            "en espanol. Devolve SOLO un JSON valido con esta forma exacta: "
            "{\"tool\": \"...\", \"params\": {...}}. "
            "Tools disponibles:\n"
            "- slack_send (params: to, message)\n"
            "- gmail_send (params: to, subject, body)\n"
            "- calendar_create (params: title, datetime, attendees)\n"
            "- reminder_create (params: when, what)"
        ),
        "user": "mandale a juan por slack que llego tarde a la reunion",
        "max_tokens": 120,
    },
    {
        "nombre": "3. Correccion con glosario",
        "system": (
            "Corregi esta transcripcion de Whisper usando el glosario. "
            "Devolve SOLO el texto corregido, sin explicaciones, manteniendo el contenido. "
            "Glosario:\n"
            "- Martin Belli (no Bechie ni Beggy)\n"
            "- MicDictado (no 'mi dictada' ni 'Mik Dictavas')\n"
            "- Santex (no Antex)\n"
            "- Llama 3.2 (no AMA)\n"
            "- tool calling (no Tooling Calling)\n"
            "- Whisper, OpenAI, RTX 2060, int8"
        ),
        "user": (
            "En la reunion del 14 de marzo, Martin Bechie presento mi dictada a "
            "Santex Group. Decidimos usar Whisper de OpenAI corriendo en local con "
            "cuantizacion int 8. La latencia bajo de 4,2 1.8 segundos es una RTX 2060. "
            "El proximo paso es integrar la AMA 3.2 con Tooling Calling."
        ),
        "max_tokens": 250,
    },
]


def ram_proceso_mb():
    if not HAY_PSUTIL:
        return 0.0
    return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024


def main():
    if not os.path.exists(MODEL_PATH_LLM):
        print(f"ERROR: no encuentro el modelo GGUF en:\n  {MODEL_PATH_LLM}")
        print("Bajalo primero con:")
        print("  curl -L -o ese_path "
              "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/"
              "resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf")
        return

    lineas = []
    def log(s=""):
        print(s)
        lineas.append(s)

    # Detectar backend disponible (CPU only o con CUDA)
    backend = "CPU"
    try:
        from llama_cpp import llama_supports_gpu_offload
        if llama_supports_gpu_offload():
            backend = "GPU (CUDA)"
    except ImportError:
        pass

    log("=" * 64)
    log(f"BENCHMARK Llama 3.2 3B Q4 + Whisper small ({backend})")
    log("=" * 64)
    log(f"Fecha: {datetime.datetime.now().isoformat(timespec='seconds')}")
    log(f"Modelo LLM:    {os.path.basename(MODEL_PATH_LLM)}")
    log(f"CPU threads:   {os.cpu_count()}")
    log(f"Backend LLM:   {backend}")
    if not HAY_PSUTIL:
        log("(psutil no instalado: no se va a medir RAM)")
    log("")

    # ── Paso 1: cargar Whisper small (simula la app real corriendo) ──
    gc.collect()
    ram_inicial = ram_proceso_mb()
    log(f"RAM antes de cargar nada:           {ram_inicial:>7.0f} MB")

    log("\n>>> Cargando Whisper small...")
    t0 = time.time()
    whisper = WhisperModel(
        "small", device="cpu", compute_type="int8", download_root=MODEL_DIR_WHISPER
    )
    t_whisper = time.time() - t0
    gc.collect()
    ram_con_whisper = ram_proceso_mb()
    log(f"  carga: {t_whisper:.1f}s")
    log(f"RAM con Whisper:                    {ram_con_whisper:>7.0f} MB"
        f"  (+{ram_con_whisper - ram_inicial:.0f})")

    # ── Paso 2: cargar LLM encima ──
    # n_gpu_layers=-1 -> ofloadea TODAS las capas a GPU si CUDA esta disponible.
    # Si llama-cpp-python esta compilado solo para CPU, este parametro se ignora.
    log("\n>>> Cargando Llama 3.2 3B Q4_K_M...")
    t0 = time.time()
    llm = Llama(
        model_path=MODEL_PATH_LLM,
        n_ctx=2048,
        n_threads=os.cpu_count(),
        n_gpu_layers=-1,
        verbose=False,
    )
    t_llm = time.time() - t0
    gc.collect()
    ram_con_ambos = ram_proceso_mb()
    log(f"  carga: {t_llm:.1f}s")
    log(f"RAM con Whisper + LLM:              {ram_con_ambos:>7.0f} MB"
        f"  (+{ram_con_ambos - ram_con_whisper:.0f})")
    log("")

    # ── Paso 3: correr cada prompt y medir ──
    resultados = []
    for p in PROMPTS:
        log("=" * 64)
        log(p["nombre"])
        log("=" * 64)
        log(f"INPUT:\n  {p['user']}")
        log("")

        t0 = time.time()
        out = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": p["system"]},
                {"role": "user", "content": p["user"]},
            ],
            max_tokens=p["max_tokens"],
            temperature=0.2,
        )
        t_total = time.time() - t0
        respuesta = out["choices"][0]["message"]["content"].strip()
        tokens_gen = out["usage"]["completion_tokens"]
        tokens_total = out["usage"]["total_tokens"]
        tps = tokens_gen / t_total if t_total > 0 else 0.0

        log(f"TIEMPO: {t_total:.2f}s  |  TOKENS: {tokens_gen} gen / {tokens_total} total"
            f"  |  {tps:.1f} tok/s")
        log("OUTPUT:")
        log(respuesta)
        log("")

        resultados.append({
            "nombre": p["nombre"],
            "t_total": t_total,
            "tokens_gen": tokens_gen,
            "tokens_total": tokens_total,
            "tps": tps,
        })

    # ── Resumen final ──
    log("=" * 64)
    log("RESUMEN")
    log("=" * 64)
    log(f"RAM inicial:                {ram_inicial:>7.0f} MB")
    log(f"RAM con Whisper:            {ram_con_whisper:>7.0f} MB"
        f"  (+{ram_con_whisper - ram_inicial:.0f})")
    log(f"RAM con Whisper + LLM:      {ram_con_ambos:>7.0f} MB"
        f"  (+{ram_con_ambos - ram_con_whisper:.0f})")
    log("")
    log(f"{'Prompt':<40}{'Tiempo':<10}{'Tokens':<10}{'tok/s':<8}")
    log("-" * 68)
    for r in resultados:
        log(f"{r['nombre']:<40}{r['t_total']:<10.2f}{r['tokens_gen']:<10}{r['tps']:<8.1f}")
    log("")
    avg_tps = sum(r["tps"] for r in resultados) / max(1, len(resultados))
    log(f"Promedio tokens/seg: {avg_tps:.1f}")
    log("")
    log("Criterios para avanzar a Fase 2 (PoC primer atajo):")
    log("  - tokens/seg > 10  -> latencia interactiva aceptable")
    log("  - RAM total < 12 GB -> la PC banca el stack")
    log("  - Output del prompt 2 (tool calling): JSON valido")
    log("  - Output del prompt 3: corrige los terminos del glosario")

    try:
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lineas) + "\n")
        print(f"\n>>> Resultados guardados en: {os.path.abspath(RESULT_FILE)}")
    except OSError as e:
        print(f"\n>>> No pude guardar el archivo: {e}")


if __name__ == "__main__":
    main()
