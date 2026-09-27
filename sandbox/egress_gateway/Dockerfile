ARG PYTHON_BASE_IMAGE=python:3.12.11-alpine3.22
ARG PYTHON_BASE_DIGEST=sha256:4e3cc5a3bccb10071da7fb8c9038d9e476d2c701cbe87af83ff369c7e2add931
FROM ${PYTHON_BASE_IMAGE}@${PYTHON_BASE_DIGEST}
COPY policy_proxy.py /usr/local/bin/policy_proxy.py
COPY policy.json /etc/egress/policy.json
RUN adduser -D -u 10001 gateway
USER gateway
EXPOSE 3128
ENTRYPOINT ["python", "/usr/local/bin/policy_proxy.py"]
