FROM node:22-bookworm-slim
# git: the whatsapp-web.js pin is a git dependency. procps: pkill for Chromium reaping.
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium git ca-certificates procps fonts-liberation \
 && rm -rf /var/lib/apt/lists/*
ENV PUPPETEER_SKIP_DOWNLOAD=1 NODE_ENV=production
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY src ./src
CMD ["node", "src/index.js"]
