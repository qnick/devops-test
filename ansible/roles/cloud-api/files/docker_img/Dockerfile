FROM alpine:3.4

ENV PYTHONUNBUFFERED=1

RUN set -x \
    && apk add --no-cache --virtual .run-deps \
        python3 \
        libffi \
        openssl \
    && apk add --no-cache --virtual .build-deps \
        python3-dev \
        musl-dev \
        gcc \
        libffi-dev \
        openssl-dev \
    && pip3 install \
        tarantool \
        ipaddress \
        docker \
        python-consul \
        python-dateutil \
        gevent flask \
        flask-restful \
        flask-bootstrap \
        flask-basicauth \
        fabric3 \
    && : "---------- remove build deps ----------" \
    && apk del .build-deps \
    && mkdir /im \
    && mkdir /im/templates \
    && mkdir /im/docker

COPY *.py /im/
COPY templates/ /im/templates/
COPY docker/ /im/docker/
COPY config/ /im/config/
WORKDIR /im

CMD ["python3", "/im/srv.py", "-c", "/im/config/config.yml"]
