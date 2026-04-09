FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       git \
       swi-prolog \
       python3 \
       python3-pip \
       python3-venv \
       coreutils \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir openai requests websocket-client

ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /opt/petta
RUN git clone https://github.com/trueagi-io/PeTTa .

COPY . /opt/petta/repos/mettaclaw
COPY scripts/run.sh /usr/local/bin/run.sh
RUN chmod +x /usr/local/bin/run.sh

CMD ["/usr/local/bin/run.sh"]
