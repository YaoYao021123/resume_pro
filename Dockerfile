FROM python:3.11-slim

# System deps for TinyTeX and fontconfig
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget perl fontconfig libfontconfig1 xz-utils poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Install TinyTeX
RUN wget -qO- "https://yihui.org/tinytex/install-bin-unix.sh" | sh \
    && mv /root/.TinyTeX /opt/TinyTeX \
    && ln -s /opt/TinyTeX/bin/*-linux*/xelatex /usr/local/bin/xelatex \
    && ln -s /opt/TinyTeX/bin/*-linux*/xetex   /usr/local/bin/xetex

# Install required LaTeX packages
RUN /opt/TinyTeX/bin/*-linux*/tlmgr install \
    xecjk ctex fontspec xetex zapfding \
    titlesec enumitem setspace hyperref \
    fancyhdr geometry cite \
    environ trimspaces

# Optional: auth/billing backend deps
RUN pip install --no-cache-dir fastapi uvicorn

# PDF parsing deps (pymupdf for CJK, pypdf as fallback)
RUN pip install --no-cache-dir pypdf pymupdf paddleocr paddlepaddle Pillow

WORKDIR /opt/resume

# Copy entire project (filtered by .dockerignore)
COPY . .

# Ensure output dir exists (will be overridden by volume mount)
RUN mkdir -p output

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["python3", "web/server.py", "--host", "0.0.0.0", "--port", "8765", "--no-open"]
