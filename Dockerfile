FROM python:3.12-slim

LABEL org.opencontainers.image.title="PDF Translate"
LABEL org.opencontainers.image.description="Tradução de PDFs com pdf2zh-next via FastAPI"

# Instala dependências de sistema necessárias para pdf2zh-next (poppler, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpoppler-cpp-dev \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Instala uv
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copia arquivos de dependências primeiro para aproveitar cache de camadas
COPY pyproject.toml uv.lock ./

# Instala dependências do projeto
RUN uv sync --no-dev --frozen

# Instala pdf2zh-next como ferramenta uv (executável em /root/.local/bin)
RUN uv tool install --python 3.12 pdf2zh-next

# Garante que os binários do uv tool estejam no PATH
ENV PATH="/root/.local/bin:$PATH"

# Copia a aplicação
COPY app.py ./
COPY static/ ./static/

# Diretório de uploads/saída (substituído pelo bind mount no compose)
RUN mkdir -p /tmp/pdf2zh_uploads

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
