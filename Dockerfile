FROM python:3.11.3-slim-bullseye

# Debian 11 (bullseye) is EOL: its security repo now 404s on deb.debian.org,
# so point apt at the archive mirror. python3-dev is not needed (the python image
# already ships the headers) and it pulled in a second, useless Python 3.9.
RUN sed -i 's|deb.debian.org/debian-security|archive.debian.org/debian-security|g; s|security.debian.org/debian-security|archive.debian.org/debian-security|g; s|deb.debian.org/debian|archive.debian.org/debian|g; /bullseye-updates/d' /etc/apt/sources.list && \
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99archive && \
    apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg gcc && \
    rm -rf /var/lib/apt/lists/*

COPY . /app/
WORKDIR /app/

RUN python -m pip install --no-cache-dir --upgrade pip setuptools && \
    python -m pip install --no-cache-dir -r requirements.txt

CMD ["python", "-m", "RoohiClone"]
