import asyncio
import json
import os
import pty
import signal
import uuid
import zipfile
from pathlib import Path
from typing import Annotated, Optional

from pypdf import PdfReader, PdfWriter

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="PDF Translate")
app.mount("/static", StaticFiles(directory="static"), name="static")

def sse_line(data: dict) -> str:
    return "data: " + json.dumps(data) + "\n\n"

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


async def _get_pdf_page_count(pdf_path: str) -> int:
    """Retorna o número de páginas do PDF via pdfinfo (poppler-utils)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pdfinfo", pdf_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode(errors="replace").splitlines():
            if line.lower().startswith("pages:"):
                return int(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return 0


def _parse_page_range(pages_str: str, total_pages: int) -> tuple[int, int]:
    """Converte '3-12' → (3, 12); '' → (1, total_pages); '5' → (5, 5).
    Para padrões complexos (vírgulas etc.) retorna (1, total_pages)."""
    s = pages_str.strip()
    if not s:
        return (1, total_pages)
    if "-" in s and "," not in s:
        parts = s.split("-", 1)
        try:
            start = int(parts[0].strip())
            end = int(parts[1].strip())
            return (max(1, start), min(end, total_pages))
        except ValueError:
            pass
    try:
        p = int(s)
        return (max(1, p), min(p, total_pages))
    except ValueError:
        pass
    return (1, total_pages)


def _extract_pages_sync(src: Path, dst: Path, start_page: int, end_page: int) -> None:
    """Extrai páginas start_page..end_page (1-indexed) de src e grava em dst."""
    reader = PdfReader(str(src))
    writer = PdfWriter()
    for i in range(start_page - 1, end_page):
        if i < len(reader.pages):
            writer.add_page(reader.pages[i])
    with open(dst, "wb") as f:
        writer.write(f)


def _merge_pdfs_sync(file_paths: list[Path], output_path: Path) -> None:
    """Concatena múltiplos PDFs em ordem usando pypdf (append preserva bookmarks/metadados)."""
    writer = PdfWriter()
    for fp in file_paths:
        if fp.exists():
            writer.append(str(fp))
    with open(output_path, "wb") as f:
        writer.write(f)


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
    batch_size: Annotated[str, Form()] = "0",
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

    # Batch config
    batch_n = max(0, int(batch_size.strip() or "0"))

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

    # Ollama host — usa o campo do form, ou cai no OLLAMA_HOST do ambiente (injetado no Docker)
    effective_ollama_host = ollama_host.strip() or os.environ.get("OLLAMA_HOST", "")
    if service == "ollama" and effective_ollama_host:
        cmd += ["--ollama-host", effective_ollama_host]

    # Pages range — ignorado em modo batch (cada batch adiciona o próprio --pages)
    if batch_n <= 0 and pages.strip():
        cmd += ["--pages", pages.strip()]

    # Workers (parallel requests to translation service)
    if workers.strip():
        cmd += ["--pool-max-workers", workers.strip()]

    # Output directory — sempre especificado para saída previsível (evita o CWD do servidor)
    effective_out_dir = output_dir.strip() or str(UPLOAD_DIR)
    cmd += ["--output", effective_out_dir]

    # Skip automatic glossary extraction (speeds up translation significantly)
    if no_auto_extract_glossary.lower() == "true":
        cmd.append("--no-auto-extract-glossary")

    # Calcula batches se necessário
    batches: list[tuple[int, int]] = []
    if batch_n > 0:
        total_pages = await _get_pdf_page_count(actual_path)
        if total_pages <= 0:
            return {"error": "Não foi possível determinar o número de páginas do PDF. Verifique se o arquivo é válido."}
        # Respeita o campo "Páginas" como intervalo máximo dos batches
        range_start, range_end = _parse_page_range(pages.strip(), total_pages)
        page = range_start
        while page <= range_end:
            batches.append((page, min(page + batch_n - 1, range_end)))
            page += batch_n

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "cmd": cmd,
        "env": {},
        "input_path": actual_path,
        "output_dir": effective_out_dir,
        "lang_out": lang_out,
        "batches": batches,
        "pages": pages.strip(),
    }

    return {"job_id": job_id, "cmd": " ".join(cmd), "batch_total": len(batches)}


@app.get("/stream/{job_id}")
async def stream_output(job_id: str):
    job = jobs.get(job_id)

    if not job:
        async def err():
            yield f"data: {json.dumps({'error': 'Job não encontrado'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    full_env = {**os.environ, **job["env"]}

    async def generate():
        batches: list[tuple[int, int]] = job.get("batches", [])
        is_batch = len(batches) > 0
        runs: list = batches if is_batch else [(None, None)]
        all_output_files: list[str] = []

        for batch_idx, batch_range in enumerate(runs):
            start_page, end_page = batch_range

            if is_batch:
                is_single_page = (start_page == end_page)
                last_page = batches[-1][1]
                if is_single_page:
                    label = f"Processando página {start_page}/{last_page}"
                    hdr_color = "\x1b[36;1m"  # ciano (igual ao modo não-batch)
                else:
                    label = f"Batch {batch_idx + 1}/{len(batches)}  (págs. {start_page}\u2013{end_page})"
                    hdr_color = "\x1b[33;1m"  # amarelo
                header = f"\r\n{hdr_color}{'─' * 4} {label} {'─' * 4}\x1b[0m\r\n\r\n"
                yield f"data: {json.dumps({'line': header})}\n\n"
                yield f"data: {json.dumps({'batch_start': True, 'batch_num': batch_idx + 1, 'batch_total': len(batches), 'page_start': start_page, 'page_end': end_page, 'single_page': is_single_page})}\n\n"
                batch_cmd = job["cmd"] + ["--pages", f"{start_page}-{end_page}"]
            else:
                batch_cmd = job["cmd"]
                # Emite cabeçalho informativo quando há restrição de páginas
                pages_field = job.get("pages", "").strip()
                if pages_field:
                    label = f"Processando págs. {pages_field}"
                    header = f"\r\n\x1b[36;1m{'─' * 4} {label} {'─' * 4}\x1b[0m\r\n\r\n"
                    yield f"data: {json.dumps({'line': header})}\n\n"
                    yield f"data: {json.dumps({'pages_start': True, 'pages': pages_field})}\n\n"

            # PTY faz isatty()=True no subprocesso → tqdm/rich exibem barras de progresso
            master_fd, slave_fd = pty.openpty()
            try:
                process = await asyncio.create_subprocess_exec(
                    *batch_cmd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    stdin=slave_fd,
                    env=full_env,
                    preexec_fn=os.setpgrp,
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
                err_line = f"Erro inesperado: {exc}\n"
                yield f"data: {json.dumps({'line': err_line, 'done': True, 'returncode': -1})}\n\n"
                return

            os.close(slave_fd)
            jobs[job_id]["process"] = process

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
                # encerra o processo (e filhos) se o cliente SSE desconectar
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except (ProcessLookupError, OSError):
                        pass

            await process.wait()

            if process.returncode != 0:
                yield f"data: {json.dumps({'done': True, 'returncode': process.returncode, 'files': all_output_files})}\n\n"
                return

            # Detecta arquivos gerados e renomeia com sufixo de batch
            in_stem = Path(job.get("input_path", "")).stem
            out_dir = Path(job.get("output_dir", str(UPLOAD_DIR)))
            batch_files: list[str] = []

            if is_batch:
                batch_tag = f"batch{batch_idx + 1:02d}"
                lang_out = job.get("lang_out", "")
                for suffix in ("mono", "dual"):
                    src = out_dir / f"{in_stem}.{lang_out}.{suffix}.pdf"
                    if not src.exists():
                        # fallback: qualquer arquivo que combine com o padrão
                        candidates = sorted(out_dir.glob(f"{in_stem}.*.{suffix}.pdf"))
                        src = candidates[0] if candidates else None  # type: ignore[assignment]
                    if src and src.exists():
                        dst = out_dir / f"{in_stem}.{lang_out}.{batch_tag}.{suffix}.pdf"
                        src.rename(dst)
                        # Extrai somente as páginas do batch (remove páginas fora do range)
                        tmp = dst.with_suffix(".tmp.pdf")
                        try:
                            await asyncio.to_thread(
                                _extract_pages_sync, dst, tmp, start_page, end_page
                            )
                            tmp.replace(dst)
                        except Exception as exc:
                            if tmp.exists():
                                tmp.unlink(missing_ok=True)
                            warn = f"Aviso: extração de páginas falhou ({exc})\r\n"
                            yield f"data: {json.dumps({'line': warn})}\n\n"
                        batch_files.append(dst.name)
                all_output_files.extend(batch_files)
                if batch_files:
                    yield f"data: {json.dumps({'batch_done': True, 'batch_num': batch_idx + 1, 'batch_total': len(batches), 'files': batch_files})}\n\n"
            else:
                for pattern in [f"{in_stem}.*.mono.pdf", f"{in_stem}.*.dual.pdf"]:
                    batch_files.extend(f.name for f in sorted(out_dir.glob(pattern)))
                # Extrai apenas as páginas especificadas no campo Pages (modo não-batch)
                pages_field = job.get("pages", "").strip()
                extract_range: tuple[int, int] | None = None
                if pages_field and "," not in pages_field:
                    if "-" in pages_field:
                        try:
                            p0, p1 = pages_field.split("-", 1)
                            extract_range = (int(p0.strip()), int(p1.strip()))
                        except ValueError:
                            pass
                    else:
                        try:
                            p = int(pages_field)
                            extract_range = (p, p)
                        except ValueError:
                            pass
                if extract_range:
                    kept: list[str] = []
                    for fname in batch_files:
                        fpath = out_dir / fname
                        if fpath.exists():
                            tmp = fpath.with_suffix(".tmp.pdf")
                            try:
                                await asyncio.to_thread(
                                    _extract_pages_sync, fpath, tmp,
                                    extract_range[0], extract_range[1]
                                )
                                tmp.replace(fpath)
                            except Exception as exc:
                                if tmp.exists():
                                    tmp.unlink(missing_ok=True)
                                warn = f"Aviso: extração de páginas falhou ({exc})\r\n"
                                yield f"data: {json.dumps({'line': warn})}\n\n"
                        kept.append(fname)
                    batch_files = kept
                all_output_files.extend(batch_files)

        jobs[job_id]["output_files"] = all_output_files
        yield f"data: {json.dumps({'done': True, 'returncode': 0, 'files': all_output_files})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/cancel/{job_id}")
async def cancel_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return {"ok": False, "reason": "Job não encontrado"}
    process = job.get("process")
    if process and process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        return {"ok": True}
    return {"ok": False, "reason": "Processo não está em execução"}


@app.post("/pause/{job_id}")
async def pause_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return {"ok": False, "reason": "Job não encontrado"}
    process = job.get("process")
    if process and process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGSTOP)
            job["paused"] = True
            return {"ok": True}
        except (ProcessLookupError, OSError) as e:
            return {"ok": False, "reason": str(e)}
    return {"ok": False, "reason": "Processo não está em execução"}


@app.post("/resume/{job_id}")
async def resume_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return {"ok": False, "reason": "Job não encontrado"}
    process = job.get("process")
    if process and process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGCONT)
            job["paused"] = False
            return {"ok": True}
        except (ProcessLookupError, OSError) as e:
            return {"ok": False, "reason": str(e)}
    return {"ok": False, "reason": "Processo não está em execução"}


@app.delete("/job/{job_id}")
async def delete_job(job_id: str):
    """Exclui os arquivos gerados por um job e remove o job do registro."""
    job = jobs.pop(job_id, None)
    if not job:
        return {"ok": False, "reason": "Job não encontrado"}
    out_dir = Path(job.get("output_dir", str(UPLOAD_DIR))).resolve()
    deleted: list[str] = []
    errors: list[str] = []
    for filename in job.get("output_files", []):
        safe_name = Path(filename).name
        if safe_name != filename or not safe_name.lower().endswith(".pdf"):
            continue
        file_path = (out_dir / safe_name).resolve()
        try:
            file_path.relative_to(out_dir)
        except ValueError:
            continue
        try:
            file_path.unlink(missing_ok=True)
            deleted.append(filename)
        except OSError as e:
            errors.append(f"{filename}: {e}")
    # Remove o arquivo ZIP se existir
    zip_path_str = job.get("zip_path")
    if zip_path_str:
        zip_path = Path(zip_path_str)
        if zip_path.parent.resolve() == out_dir and zip_path.suffix == ".zip":
            zip_path.unlink(missing_ok=True)
    # Remove PDFs mesclados e ZIP de merge se existirem
    for merged_fname in job.get("merged_files", []):
        mp = Path(merged_fname)
        if mp.parent.resolve() == out_dir and mp.suffix == ".pdf":
            mp.unlink(missing_ok=True)
    merge_zip_str = job.get("merge_zip_path")
    if merge_zip_str:
        mzp = Path(merge_zip_str)
        if mzp.parent.resolve() == out_dir and mzp.suffix == ".zip":
            mzp.unlink(missing_ok=True)
    return {"ok": True, "deleted": deleted, "errors": errors}


@app.get("/download/{job_id}/{filename}")
async def download_file(job_id: str, filename: str):
    job = jobs.get(job_id)
    if not job:
        return {"error": "Job não encontrado"}
    # Segurança: rejeita path traversal e arquivos que não sejam PDF
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith(".pdf"):
        return {"error": "Nome de arquivo inválido"}
    out_dir = Path(job.get("output_dir", str(UPLOAD_DIR))).resolve()
    file_path = (out_dir / safe_name).resolve()
    # Segurança: garante que o arquivo está dentro do diretório permitido
    try:
        file_path.relative_to(out_dir)
    except ValueError:
        return {"error": "Acesso negado"}
    if not file_path.exists():
        return {"error": "Arquivo não encontrado"}
    return FileResponse(path=file_path, filename=safe_name, media_type="application/pdf")


@app.get("/zip/stream/{job_id}")
async def zip_stream(job_id: str):
    """Cria um ZIP flat com todos os arquivos gerados, emitindo progresso via SSE."""
    job = jobs.get(job_id)

    async def generate():
        if not job:
            yield sse_line({'zip_error': 'Job não encontrado'})
            return

        output_files: list[str] = job.get("output_files", [])
        if not output_files:
            yield sse_line({'line': '\r\n\x1b[31mNenhum arquivo gerado para compactar.\x1b[0m\r\n'})
            yield sse_line({'zip_error': 'Sem arquivos'})
            return

        out_dir = Path(job.get("output_dir", str(UPLOAD_DIR))).resolve()
        zip_filename = f"traducao_{job_id[:8]}.zip"
        zip_path = out_dir / zip_filename

        yield sse_line({'line': '\r\n\x1b[36;1m──── Compactando arquivos ────\x1b[0m\r\n\r\n'})

        added = 0
        loop = asyncio.get_event_loop()
        try:
            zf = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED)
            for file_str in output_files:
                safe_name = Path(file_str).name
                file_path = (out_dir / safe_name).resolve()
                try:
                    file_path.relative_to(out_dir)
                except ValueError:
                    continue
                if not file_path.exists():
                    continue
                yield sse_line({'line': f'  \x1b[90m+ {safe_name}\x1b[0m\r\n'})
                await loop.run_in_executor(None, zf.write, file_path, safe_name)
                added += 1
            await loop.run_in_executor(None, zf.close)
        except Exception as exc:
            yield sse_line({'line': f'\r\n\x1b[31mErro: {exc}\x1b[0m\r\n'})
            yield sse_line({'zip_error': str(exc)})
            return

        size_mb = zip_path.stat().st_size / (1024 * 1024)
        yield sse_line({'line': f'\r\n\x1b[32;1m\u2713 ZIP criado com {added} arquivo(s) ({size_mb:.1f}\u00a0MB)\x1b[0m\r\n'})
        job["zip_path"] = str(zip_path)
        yield sse_line({'zip_ready': True, 'filename': zip_filename})

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/zip/download/{job_id}")
async def zip_download(job_id: str):
    """Retorna o arquivo ZIP previamente criado para o job."""
    from fastapi import HTTPException
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    zip_path_str = job.get("zip_path")
    if not zip_path_str:
        raise HTTPException(status_code=404, detail="ZIP ainda não criado — use /zip/stream/{job_id} primeiro")
    zip_path = Path(zip_path_str)
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo ZIP não encontrado no servidor")
    return FileResponse(path=zip_path, filename=zip_path.name, media_type="application/zip")


@app.get("/merge/stream/{job_id}")
async def merge_stream(job_id: str):
    """Concatena Mono e Dual separadamente, zipa os dois e emite progresso via SSE."""
    job = jobs.get(job_id)

    async def generate():
        if not job:
            yield sse_line({'merge_error': 'Job não encontrado'})
            return

        output_files: list[str] = job.get("output_files", [])
        if not output_files:
            yield sse_line({'line': '\r\n\x1b[31mNenhum arquivo gerado para juntar.\x1b[0m\r\n'})
            yield sse_line({'merge_error': 'Sem arquivos'})
            return

        out_dir = Path(job.get("output_dir", str(UPLOAD_DIR))).resolve()
        in_stem = Path(job.get("input_path", "arquivo")).stem
        lang_out = job.get("lang_out", "")

        # Separa e ordena pelo nome (batch01 < batch02 … ordenação lexicográfica com zero-pad)
        mono_files = sorted(
            [f for f in output_files if f.lower().endswith(".mono.pdf")],
        )
        dual_files = sorted(
            [f for f in output_files if f.lower().endswith(".dual.pdf")],
        )

        if not mono_files and not dual_files:
            yield sse_line({'line': '\r\n\x1b[31mNenhum arquivo Mono ou Dual encontrado.\x1b[0m\r\n'})
            yield sse_line({'merge_error': 'Sem arquivos Mono/Dual'})
            return

        yield sse_line({'line': '\r\n\x1b[36;1m──── Juntando PDFs ────\x1b[0m\r\n\r\n'})

        loop = asyncio.get_event_loop()
        merged_paths: list[Path] = []

        for group_label, files in (("Mono", mono_files), ("Dual", dual_files)):
            if not files:
                continue
            suffix = group_label.lower()
            yield sse_line({'line': f'  \x1b[33m\u25b6 {group_label} \u2014 {len(files)} arquivo(s):\x1b[0m\r\n'})
            for fname in files:
                yield sse_line({'line': f'    \x1b[90m+ {fname}\x1b[0m\r\n'})

            out_name = f"{in_stem}.{lang_out}.merged.{suffix}.pdf" if lang_out else f"{in_stem}.merged.{suffix}.pdf"
            out_path = out_dir / out_name

            # resolve caminhos com validação de path traversal
            valid_paths: list[Path] = []
            for fname in files:
                safe_name = Path(fname).name
                fp = (out_dir / safe_name).resolve()
                try:
                    fp.relative_to(out_dir)
                except ValueError:
                    continue
                valid_paths.append(fp)

            yield sse_line({'line': f'    \x1b[90mMesclando\u2026\x1b[0m\r\n'})
            try:
                await loop.run_in_executor(None, _merge_pdfs_sync, valid_paths, out_path)
            except Exception as exc:
                yield sse_line({'line': f'\r\n\x1b[31mErro ao mesclar {group_label}: {exc}\x1b[0m\r\n'})
                yield sse_line({'merge_error': str(exc)})
                return

            merged_paths.append(out_path)
            yield sse_line({'line': f'    \x1b[32m\u2713 {out_name}\x1b[0m\r\n\r\n'})

        # Cria o ZIP com os PDFs mesclados
        zip_filename = f"traducao_{job_id[:8]}.merged.zip"
        zip_path = out_dir / zip_filename

        yield sse_line({'line': '\r\n\x1b[36;1m──── Compactando ────\x1b[0m\r\n\r\n'})
        try:
            zf = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED)
            for mp in merged_paths:
                yield sse_line({'line': f'  \x1b[90m+ {mp.name}\x1b[0m\r\n'})
                await loop.run_in_executor(None, zf.write, mp, mp.name)
            await loop.run_in_executor(None, zf.close)
        except Exception as exc:
            yield sse_line({'line': f'\r\n\x1b[31mErro ao criar ZIP: {exc}\x1b[0m\r\n'})
            yield sse_line({'merge_error': str(exc)})
            return

        size_mb = zip_path.stat().st_size / (1024 * 1024)
        yield sse_line({'line': f'\r\n\x1b[32;1m\u2713 Pronto! {len(merged_paths)} PDF(s) mesclado(s) ({size_mb:.1f}\u00a0MB)\x1b[0m\r\n'})
        job["merge_zip_path"] = str(zip_path)
        job["merged_files"] = [str(p) for p in merged_paths]
        yield sse_line({'merge_ready': True, 'filename': zip_filename})

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/merge/download/{job_id}")
async def merge_download(job_id: str):
    """Retorna o ZIP com os PDFs mesclados."""
    from fastapi import HTTPException
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    zip_path_str = job.get("merge_zip_path")
    if not zip_path_str:
        raise HTTPException(status_code=404, detail="Merge ZIP ainda não criado — use /merge/stream/{job_id} primeiro")
    zip_path = Path(zip_path_str)
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo ZIP de merge não encontrado no servidor")
    return FileResponse(path=zip_path, filename=zip_path.name, media_type="application/zip")
