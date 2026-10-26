FROM python:3.15-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Hash-verified PSRT bootstrap also replaces pip's independently bundled CAs.
RUN python -m pip install --no-cache-dir --upgrade "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py logging_config.py ./

# Dependencies are installed above; the service never invokes pip at runtime.
RUN python -m pip uninstall --yes pip

RUN useradd -u 10001 -m app
USER 10001

CMD ["python", "bot.py"]
