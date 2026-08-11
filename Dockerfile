FROM public.ecr.aws/docker/library/python:3.12-slim

# AWS Lambda Web Adapter — lets a plain HTTP app run behind a function URL
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY web/ web/
COPY certs/root.crt /app/certs/root.crt
RUN chmod -R a+rX /app/certs

ENV PORT=8080 AWS_LWA_INVOKE_MODE=buffered
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "fleetmemory.web:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8080"]
