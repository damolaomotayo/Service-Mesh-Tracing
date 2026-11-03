FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY service.py .
RUN useradd --system --uid 10001 appuser
USER appuser
CMD ["python", "service.py"]
