FROM python:3.12-slim
WORKDIR /app
COPY server.py calendar.json ./
ENV STATE_FILE=/data/state.json
RUN mkdir /data
EXPOSE 8080
CMD ["python", "server.py"]
