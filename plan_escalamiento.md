# Plan de Escalamiento — MicDictado

> Última actualización: 2026-04-29
> Estado: definiendo dirección de producto, pendiente correr benchmark.

## Estado actual

App de dictado por voz local en Windows, 100% offline:

- **Stack**: Python + faster-whisper (Whisper de OpenAI con CTranslate2) corriendo en CPU con cuantización int8.
- **Modelo activo**: `small` (configurable a `tiny | base | medium`).
- **Diferenciadores reales**:
  1. 100% local — el audio nunca sale de la máquina.
  2. Pegado universal con SendInput Unicode (funciona en terminales y Electron, donde la mayoría de competidores fallan).
  3. Sin suscripción.
- **Features recientes** (commit pendiente):
  - `initial_prompt` configurable para sesgar Whisper hacia vocabulario propio (nombres, marcas, jerga técnica).
  - Historial local de transcripciones en JSONL, con rotación y UI para abrir/borrar.

## Análisis de mercado

El espacio "dictado a texto" está saturado: Wispr Flow, SuperWhisper, MacWhisper, Dragon, dictado nativo de Windows, etc. **No competir en "transcribir audio"** — competir en algo que esos no hacen bien.

## Tres frentes posibles, ordenados por viabilidad

### 1. Atajos por voz con APIs directas + LLM local **(foco principal)**

> "Mandale por Slack a Juan que llego tarde" → API directa, sin tocar mouse ni teclado.

Mucho más confiable que Computer Use (mouse/teclado se rompe con cualquier cambio de UI).

Flujo:
```
voz → Whisper → texto → LLM local con tool calling → llamada directa a API
```

Casos típicos:
- Slack/Discord/Teams: mandar mensajes
- Gmail: mandar mail, buscar, resumir últimos N
- Google Calendar: agendar, ver agenda del día
- Notion/Obsidian: crear nota, buscar
- Recordatorios locales
- Búsqueda local de archivos
- Control de Spotify

**Por qué este eje**: aprovecha todo lo construido, es claramente diferente de Wispr Flow, y el patrón "1 atajo funcionando" se replica fácil a 10+.

### 2. Wispr Flow offline (reescritura con LLM local)

Voz cruda → Whisper → LLM local reescribe con formato (email formal, ticket Jira, mensaje Slack profesional). **Todo local**, mismo stack que el frente 1. Complementario, no separado.

### 3. Móvil tipo Plaud (descartado como producto)

Para uso personal: comprar Plaud (~USD 179). Armarlo desde cero son 3–6 meses de app móvil nativa con wake word, Whisper en mobile, recording en background. El espacio ya tiene Plaud, Limitless Pendant, Bee, Friend, Granola — y los grandes (Apple Intelligence, Google) lo van a integrar nativo.

**Si igual hubiera nicho**: app móvil simple, sin wake word, solo botón. Graba local en celu, transcribe local con Whisper mobile, manda solo el texto a PC/cloud para procesar. Privacidad como diferencial. Pero implica abrir un nuevo proyecto entero.

## Roadmap operativo

### Fase 0 — Benchmark de hardware (en curso)

- [x] Script `bench_whisper.py` armado (compara `small` vs `medium`: latencia, RAM, calidad).
- [ ] Correr benchmark en la máquina actual (Ryzen 5 4600G, 20 GB, RTX 2060).
- [ ] Decidir si cambiar default de `small` a `medium`.

**Criterio**: si `medium` corre > 1.0x realtime y suma < 1.5 GB extra de RAM, vale el cambio.

### Fase 1 — LLM local

- [ ] Instalar `llama-cpp-python`.
- [ ] Descargar Llama 3.2 3B Instruct Q4_K_M (~2 GB) en formato GGUF.
- [ ] Benchmark: tokens/seg, RAM con Whisper + LLM cargados simultáneo, latencia de respuesta corta.
- [ ] Validar que la PC banca ambos modelos en paralelo sin swap.

**Criterio**: respuesta < 3 seg para prompts cortos en CPU.

### Fase 2 — PoC primer atajo (Slack)

- [ ] Definir gramática de comandos: hotkey distinto (¿Ctrl izq + barra?) que dispara modo "comando" vs modo "dictado".
- [ ] Intent + parámetros con LLM local con tool calling.
- [ ] Integración con Slack API (token de workspace propio).
- [ ] Resolución de destinatario por nombre ("Juan" → user ID).
- [ ] Confirmación antes de enviar (UX a definir).

### Fase 3 — Más atajos siguiendo el mismo patrón

Gmail, Google Calendar, Notion. Cada uno = nuevo "tool" que el LLM puede elegir.

### Fase 4 — Producto

- Decidir si seguir solo (uso personal + side project) o packaging para vender.
- Si va a venta: vertical compliance (médicos, legal) o productividad general.
- Modelo: pago único + opcional cloud para features pesados.

## Requisitos técnicos y TAM

| Tier | Specs | % parque PCs | Whisper viable | LLM local viable |
|------|-------|--------------|----------------|------------------|
| Bajo | i3/Ryzen 3, 8 GB, sin GPU dedicada | ~60–70% | `tiny`/`base` (calidad floja en español) | Phi-3.5 mini Q4 con paciencia |
| Medio | i5/Ryzen 5 gen 11+, 16 GB, GPU integrada moderna o entrada | ~25–30% | `small` cómodo, `medium` aceptable | Llama 3.2 3B Q4 cómodo |
| Alto | i7/Ryzen 7, 32 GB, RTX 4060+, o Mac M-series | ~5% | `medium`/`large-v3` sin drama | 7B–14B fluido |
| Premium 2026 | NPU (Snapdragon X, Lunar Lake) o Apple Silicon | <5% pero crece | Todo en NPU sin batería | 14B local de pie |

**Decisión**: apuntar a tier medio. Cubre ~25–30% del parque + casi todos los profesionales que pagarían. Tier bajo sacrifica calidad y rompe la experiencia.

## Hardware actual y recomendación

Máquina actual: **Ryzen 5 4600G, 20 GB RAM, RTX 2060 4 GB VRAM**.

Tier medio holgado (alto si contás la GPU). **No comprar nada todavía** — la PC banca todo lo del roadmap. Reevaluar después del benchmark.

Si llegado el momento se necesita upgrade, en orden de costo-beneficio:

1. **RAM 32 GB** (~USD 60–80) — el upgrade más barato y más sentido al correr Whisper + LLM en paralelo.
2. **Mac mini M4 16 GB** (~USD 600) — banco de pruebas imbatible para inference local por unified memory + Neural Engine. No reemplaza la PC Windows (la app usa SendInput).
3. **Notebook Copilot+ PC** (Snapdragon X / Lunar Lake, USD 1000–1400) — portabilidad + NPU. Ecosistema en Windows todavía verde, mejorará en 2026.
4. **PC con RTX 4070 Super** (~USD 1500 total) — overkill para producto, ideal si entrenamiento o 13B+.

## Decisiones tomadas

- **No competir en transcripción cruda** — espacio saturado.
- **Foco principal: atajos por voz con APIs**, no Computer Use.
- **LLM local objetivo**: Llama 3.2 3B Q4_K_M (sweet spot capacidad / RAM).
- **Plaud**: comprar si se necesita personalmente, no construir.
- **Tier medio** como target de hardware del producto.
- **No comprar PC nueva** hasta tener números del benchmark.

## Pendientes inmediatos

1. Correr `bench_whisper.py`, documentar resultado al final de este archivo.
2. Decidir Fase 1 según resultado.
