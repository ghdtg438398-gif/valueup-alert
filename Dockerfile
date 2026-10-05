FROM python:3.12-slim
ENV TZ=Asia/Seoul PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY run.py .
EXPOSE 8000
CMD ["python", "run.py"]
