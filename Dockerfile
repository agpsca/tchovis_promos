# Imagem oficial do Playwright, com navegadores e bibliotecas de sistema
# na mesma versao da biblioteca Python fixada em requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TCHOVIS_STATE_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher.py config.json ./

# O diretorio /data recebe o volume com o historico em SQLite.
RUN mkdir -p /data && chown -R pwuser:pwuser /app /data

USER pwuser

ENTRYPOINT ["python", "watcher.py"]
