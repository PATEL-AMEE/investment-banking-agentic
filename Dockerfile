FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./

# Non-root runtime (AKS pod securityContext: runAsNonRoot). The app writes
# uploads/ and data/audit at runtime, so those must belong to the app user.
RUN useradd --uid 10001 --create-home appuser \
    && mkdir -p uploads data/audit \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
