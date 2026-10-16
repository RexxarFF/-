FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN useradd --create-home --uid 10001 diary && mkdir -p /app/data && chown -R diary:diary /app
USER diary
ENV PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
CMD ["python", "-u", "main.py"]
