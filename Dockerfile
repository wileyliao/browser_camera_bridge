FROM python:3.10-slim

WORKDIR /app

# 安裝依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 放程式
COPY ws_bridge.py .

CMD ["python", "ws_bridge.py"]

# docker run -d --name expo_ws_bridge --net ai --restart always -p 8080:8080 -p 9001:9001  wjcl3gj94/toolkitmcam:v1.0