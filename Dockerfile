ARG package=app_outage_plugin

# build poetry
FROM quay.io/centos/centos:stream8 as poetry
ARG package
RUN dnf -y module install python39 && dnf -y install python39 python39-pip

WORKDIR /app

COPY poetry.lock /app/
COPY pyproject.toml /app/
COPY ${package}/ /app/${package}
COPY README.md /app/

RUN python3.9 -m pip install poetry 
RUN python3.9 -m poetry config virtualenvs.create false 
RUN python3.9 -m poetry install --no-interaction --no-ansi 
RUN python3.9 -m poetry export -f requirements.txt --output requirements.txt --without-hashes

# run tests
COPY tests /app/tests

RUN mkdir /htmlcov
RUN pip3 install coverage
RUN python3 -m coverage run tests/test_app_outage_plugin.py
RUN python3 -m coverage html -d /htmlcov --omit=/usr/local/*


# final image
FROM quay.io/centos/centos:stream8
ARG package
RUN dnf -y module install python39 && dnf -y install python39 python39-pip

WORKDIR /app

COPY --from=poetry /app/requirements.txt /app/
COPY --from=poetry /htmlcov /htmlcov/
COPY README.md /app/
COPY ${package}/ /app/${package}

RUN python3.9 -m pip install -r requirements.txt


ENTRYPOINT ["python3", "-m","app_outage_plugin.app_outage_plugin"]
CMD []

LABEL org.opencontainers.image.source="https://github.com/yogananth-subramanian/app_outage_plugin.git"
LABEL org.opencontainers.image.licenses="Apache-2.0+GPL-2.0-only"
LABEL org.opencontainers.image.vendor="Arcalot project"
LABEL org.opencontainers.image.authors="Arcalot contributors"
LABEL org.opencontainers.image.title="Chaos Engineering App outage Plugin for Arcaflow"
LABEL io.github.arcalot.arcaflow.plugin.version="1"
