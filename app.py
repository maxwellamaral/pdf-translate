import asyncio
import json
import os
import pty
import uuid
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="PDF Translate")
app.mount("/static", StaticFiles(directory="static"), name="static")

UPLOAD_DIR = Path("/tmp/pdf2zh_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

jobs: dict[str, dict] = {}

# pdf2zh-next (v2) — cada serviço vira uma flag própria
SERVICE_FLAGS: dict[str, str] = {
    "ollama":           "--ollama",
    "openai":           "--openai",
    "google":           "--google",
    "bing":             "--bing",
    "deepseek":         "--deepseek",
    "gemini":           "--gemini",
    "groq":             "--groq",
    "grok":             "--grok",
    "deepl":            "--deepl",
    "openaicompatible": "--openaicompatible",
}

SERVICE_MODEL_FLAGS: dict[str, str] = {
    "ollama":           "--ollama-model",
    "openai":           "--openai-model",
    "deepseek":         "--deepseek-model",
    "gemini":           "--gemini-model",
    "groq":             "--groq-model",
    "grok":             "--grok-model",
    "openaicompatible": "--openai-compatible-model",
}


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path("static/index.html").read_text()


@app.post("/translate")
async def translate(
    file_path: Annotated[str, Form()] = "",
    file: Annotated[Optional[UploadFile], File()] = None,
    lang_in: Annotated[str, Form()] = "en",
    lang_out: Annotated[str, Form()] = "pt",
    service: Annotated[str, Form()] = "ollama",
    model: Annotated[str, Form()] = "",
    pages: Annotated[str, Form()] = "",
    workers: Annotated[str, Form()] = "1",
    output_dir: Annotated[str, Form()] = "",
    ollama_host: Annotated[str, Form()] = "",
    no_auto_extract_glossary: Annotated[str, Form()] = "true",
):
    # Resolve file path
    actual_path = file_path.strip()
    if file and file.filename:
        safe_name = Path(file.filename).name  # prevent path traversal
        dest = UPLOAD_DIR / safe_name
        dest.write_bytes(await file.read())
        actual_path = str(dest)

    if not actual_path:
        return {"error": "Nenhum arquivo fornecido"}

    # Build pdf2zh-next (v2) command
    cmd = ["pdf2zh", actual_path, "--lang-in", lang_in, "--lang-out", lang_out]

    # Service flag
    service_flag = SERVICE_FLAGS.get(service)
    if service_flag:
        cmd.append(service_flag)

    # Model flag (if applicable)
    model_clean = model.strip()
    model_flag = SERVICE_MODEL_FLAGS.get(service)
    if model_flag and model_clean:
        cmd += [model_flag, model_clean]

    # Ollama host
    if service == "ollama" and ollama_host.strip():
        cmd += ["--ollama-host", ollama_host.strip()]

    # Pages range
    if pages.strip():
        cmd += ["--pages", pages.strip()]

    # Workers (parallel requests to translation service)
    if workers.strip():
        cmd += ["--pool-max-workers", workers.strip()]

    # Output directory
    if output_dir.strip():
        cmd += ["--output", output_dir.strip()]

    # Skip automatic glossary extraction (speeds up translation significantly)
    if no_auto_extract_glossary.lower() == "true":
        cmd.append("--no-auto-extract-glossary")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"cmd": cmd, "env": {}}

    return {"job_id": job_id, "cmd": " ".join(cmd)}


@app.get("/stream/{job_id}")
async def stream_output(job_id: str):
    job = jobs.get(job_id)

    if not job:
        async def err():
            yield f"data: {json.dumps({'error': 'Job não encontrado'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    full_env = {**os.environ, **job["env"]}

    async def generate():
        # PTY faz isatty()=True no subprocesso → tqdm/rich exibem barras de progresso
        master_fd, slave_fd = pty.openpty()
        try:
            process = await asyncio.create_subprocess_exec(
                *job["cmd"],
                stdout=slave_fd,
                stderr=slave_fd,
                stdin=slave_fd,
                env=full_env,
            )
        except FileNotFoundError:
            os.close(slave_fd)
            os.close(master_fd)
            msg = "Erro: 'pdf2zh' não encontrado no PATH.\nInstale com: uv tool install --python 3.12 pdf2zh-next\n"
            yield f"data: {json.dumps({'line': msg, 'done': True, 'returncode': 127})}\n\n"
            return
        except Exception as exc:
            os.close(slave_fd)
            os.close(master_fd)
            yield f"data: {json.dumps({'line': f'Erro inesperado: {exc}\n', 'done': True, 'returncode': -1})}\n\n"
            return

        os.close(slave_fd)  # fecha o lado escravo no processo pai

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _reader():
            try:
                data = os.read(master_fd, 4096)
                queue.put_nowait(data if data else None)
                if not data:
                    loop.remove_reader(master_fd)
            except OSError:
                queue.put_nowait(None)
                loop.remove_reader(master_fd)

        loop.add_reader(master_fd, _reader)
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield f"data: {json.dumps({'line': chunk.decode('utf-8', errors='replace')})}\n\n"
        finally:
            loop.remove_reader(master_fd)
            try:
                os.close(master_fd)
            except OSError:
                pass
            # encerra o processo se o cliente SSE desconectar
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

        await process.wait()
        yield f"data: {json.dumps({'done': True, 'returncode': process.returncode})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
