FROM ubuntu:22.04

# Evitar preguntas interactivas en instalación
ENV DEBIAN_FRONTEND=noninteractive

# Instalar herramientas necesarias
RUN apt update && apt install -y \
    iputils-ping \
    iproute2 \
    net-tools \
    iperf3 \
    hping3 \
    tcpdump \
    tshark \
    tcpreplay \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Configurar parámetros de red para aumentar buffers de recepción
RUN echo "net.core.rmem_max=26214400" >> /etc/sysctl.conf
RUN echo "net.core.rmem_default=26214400" >> /etc/sysctl.conf

# Comando por defecto al iniciar el contenedor
CMD ["/bin/bash"]
