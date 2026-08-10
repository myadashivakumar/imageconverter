# Lambda container image. Build for x86_64 to match a standard Lambda function:
#   docker build --platform linux/amd64 -t imageconvertfastapi .
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r requirements.txt

COPY app ${LAMBDA_TASK_ROOT}/app

CMD ["app.main.handler"]
