FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir '.[mesh]'
ENTRYPOINT ["veldbridge", "--config", "/etc/veld-bridge/config.yaml"]
