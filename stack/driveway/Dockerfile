# The driveway watcher (optional, profile "driveway"): reads the driveway camera only while the
# site asks, during a car's approach home. ffmpeg pulls frames; YOLOX-S (Apache 2.0) finds cars.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir onnxruntime==1.20.1 numpy==2.1.3 pillow==11.0.0
WORKDIR /app
RUN curl -sSfL -o yolox_s.onnx https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx \
 && echo "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063  yolox_s.onnx" | sha256sum -c -
COPY driveway.py ./
RUN mkdir -p /data && chown 1000 /data
USER 1000
ENTRYPOINT ["python", "driveway.py"]
CMD ["serve"]
