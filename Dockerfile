FROM python:3 AS python_base

WORKDIR /usr/src/app

COPY . .

FROM python_base AS prog_runtime

ENV BIND_PORT=8080
EXPOSE ${BIND_PORT}/tcp

CMD [ "python3", "./met-proxy.py" ]
