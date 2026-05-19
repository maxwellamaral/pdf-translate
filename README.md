# PDF Translate

Interface web para tradução de PDFs acadêmicos com preservação de formatação, equações e layout de duas colunas. Utiliza o [pdf2zh-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next) como motor de tradução e suporta modelos locais via [Ollama](https://ollama.com/).

## Funcionalidades

- Upload de PDF por arrastar e soltar ou por caminho local
- Suporte a múltiplos serviços: Ollama (local), OpenAI, Gemini, DeepSeek, Groq, Grok, DeepL, Google, Bing
- Saída bilíngue (original + tradução) e monolingue
- Streaming do log de progresso em tempo real no navegador
- Configuração de range de páginas, número de workers e diretório de saída

---

## Requisitos

- [Python 3.12+](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/) — gerenciador de pacotes/projetos
- [Ollama](https://ollama.com/) (para uso local)

---

## Instalação

### 1. Instale o pdf2zh-next

```bash
uv tool install --python 3.12 pdf2zh-next
```

### 2. Clone o repositório e instale as dependências

```bash
git clone <url-do-repositorio>
cd pdf-translate
uv sync
```

### 3. (Ollama) Baixe o modelo de tradução

```bash
ollama pull translategemma:4b
```

---

## Uso

### Iniciar o servidor

```bash
chmod +x start.sh
./start.sh
```

O servidor estará disponível em `http://localhost:8000`.

Variáveis de ambiente opcionais:

| Variável | Padrão    | Descrição            |
|----------|-----------|----------------------|
| `HOST`   | `0.0.0.0` | Endereço de escuta   |
| `PORT`   | `8000`    | Porta TCP            |

### Uso via linha de comando (sem a interface web)

```bash
# Traduzir artigo completo com Ollama (GPU)
pdf2zh artigo.pdf \
  --ollama \
  --ollama-model translategemma:4b \
  --lang-in en \
  --lang-out pt \
  --no-auto-extract-glossary \
  --pool-max-workers 1

# Traduzir apenas as primeiras 5 páginas
pdf2zh artigo.pdf \
  --ollama \
  --ollama-model translategemma:4b \
  --lang-in en \
  --lang-out pt \
  --no-auto-extract-glossary \
  --pool-max-workers 1 \
  --pages 1-5
```

Os arquivos de saída são gerados no mesmo diretório do PDF de entrada:
- `artigo-mono.pdf` — tradução monolingue
- `artigo-dual.pdf` — documento bilíngue (original + tradução)

---

## Configuração com Ollama local

### Verificar se o Ollama está usando a GPU

```bash
ollama ps
# A coluna PROCESSOR deve mostrar "100% GPU"
```

### Modelos recomendados

| Modelo                  | VRAM aprox. | Velocidade | Qualidade |
|-------------------------|-------------|------------|-----------|
| `translategemma:4b`     | ~4.3 GB     | Média      | Alta (especializado em tradução) |
| `qwen2.5:3b`            | ~2.0 GB     | Alta       | Boa       |
| `gemma3:4b`             | ~3.0 GB     | Alta       | Boa       |

### Dicas de performance

- **Workers = 1** é o mais eficiente para Ollama local (evita contenção na GPU)
- **`--no-auto-extract-glossary`** ativado reduz o tempo em ~30%
- Artigos de 13 páginas em duas colunas levam aproximadamente 4–8 minutos com uma RTX 4060

---

## Estrutura do projeto

```
pdf-translate/
├── app.py              # Backend FastAPI
├── static/
│   └── index.html      # Interface web (Tailwind CSS)
├── pyproject.toml      # Dependências Python (uv)
├── start.sh            # Script de inicialização
└── README.md
```

---

## Licença

Copyright © 2024 **Maxwell Anderson Ielpo do Amaral**

Este software é de livre uso, modificação e distribuição, desde que o autor original seja referenciado em qualquer trabalho derivado, publicação ou redistribuição.

Redistribuições em formato binário ou código-fonte devem reproduzir o aviso de copyright acima e esta lista de condições.

O autor não oferece garantias de qualquer tipo sobre o software e não é responsável por danos decorrentes de seu uso.

---

## Citação

Se você utilizar este software em pesquisa acadêmica, por favor cite:

```bibtex
@software{amaral2024pdftranslate,
  author       = {Amaral, Maxwell Anderson Ielpo do},
  title        = {{PDF Translate}: Interface web para tradução de PDFs acadêmicos com pdf2zh-next},
  year         = {2024},
  url          = {https://github.com/maxwellamaral/pdf-translate},
  note         = {Motor de tradução: pdf2zh-next (PDFMathTranslate). Modelos locais via Ollama.},
  license      = {Livre uso com atribuição}
}
```
