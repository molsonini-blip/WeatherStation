FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/
COPY tools/ /tools/
VOLUME /data
EXPOSE 8088/tcp 50222/udp
CMD ["python", "/app/monitor.py"]
