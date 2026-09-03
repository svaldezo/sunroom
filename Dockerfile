# The container path. Same code as the serverless one; the difference is that
# here the process outlives a request, so the worker runs inline instead of
# being driven by cron. Use this if the serverless time limits ever bite, or to
# run Sunroom on your own machine.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall the world.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt uvicorn[standard]

COPY prism ./prism
COPY supabase ./supabase
COPY pyproject.toml README.md ./

# Not root. A container that runs the whole application as uid 0 is one
# container escape away from being a much worse day.
RUN useradd --create-home --uid 10001 sunroom \
 && mkdir -p /data && chown -R sunroom:sunroom /data /app
USER sunroom

ENV PRISM_HOME=/data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "prism.web.api:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--log-level", "warning"]
