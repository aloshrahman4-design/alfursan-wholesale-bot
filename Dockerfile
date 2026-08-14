FROM python:3.12-slim

# Node.js 20.x on top of the Debian/glibc base (glibc is required for Pillow's
# bundled raqm/harfbuzz wheel used by the price bot's Arabic text rendering).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg git libraqm0 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# @whiskeysockets/baileys depends on libsignal via a git+ssh GitHub URL; no
# SSH key is available in the build, so rewrite it to anonymous HTTPS.
RUN git config --global url."https://github.com/".insteadOf "git@github.com:" \
    && git config --global url."https://github.com/".insteadOf "ssh://git@github.com/"

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --omit=dev

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=10000
EXPOSE 10000

CMD ["node", "orchestrator.js"]
