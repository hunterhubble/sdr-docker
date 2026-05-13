FROM ubuntu:22.04

# Workaround for hash-sum-mismatch on ARM mirrors
RUN echo 'Acquire::http::Pipeline-Depth "0";' > /etc/apt/apt.conf.d/99fixmirror && \
    echo 'Acquire::http::No-Cache=True;' >> /etc/apt/apt.conf.d/99fixmirror && \
    echo 'Acquire::BrokenProxy=true;' >> /etc/apt/apt.conf.d/99fixmirror

# Install prerequisites
RUN rm -rf /var/lib/apt/lists/* && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        software-properties-common wget python3-pip git cmake && \
    rm -rf /var/lib/apt/lists/*

# Add the GNU Radio PPA repository and install GNU Radio
RUN add-apt-repository -y ppa:gnuradio/gnuradio-releases && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --fix-missing gnuradio gnuradio-dev

# Download and install libiio (PlutoSDR support)
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; then \
        DEB="libiio-0.26.g-Ubuntu-arm64v8.deb"; \
    else \
        DEB="libiio-0.26.ga0eca0d-Linux-Ubuntu-22.04.deb"; \
    fi && \
    wget "https://github.com/analogdevicesinc/libiio/releases/download/v0.26/$DEB" && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y "./$DEB" && \
    rm "$DEB"

# Install SoapySDR + bladeRF + PlutoSDR SoapySDR modules
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        libsoapysdr-dev python3-soapysdr soapysdr-tools \
        libbladerf-dev libbladerf2 bladerf && \
    rm -rf /var/lib/apt/lists/*

# Build SoapyPlutoSDR module from source (PlutoSDR via gr-soapy)
RUN git clone --depth 1 https://github.com/pothosware/SoapyPlutoSDR.git /tmp/SoapyPlutoSDR && \
    cd /tmp/SoapyPlutoSDR && mkdir build && cd build && \
    cmake .. && make -j"$(nproc)" && make install && \
    rm -rf /tmp/SoapyPlutoSDR && \
    ldconfig

# Build SoapyBladeRF module from source (bladeRF via gr-soapy)
RUN git clone --depth 1 https://github.com/pothosware/SoapyBladeRF.git /tmp/SoapyBladeRF && \
    cd /tmp/SoapyBladeRF && mkdir build && cd build && \
    cmake .. && make -j"$(nproc)" && make install && \
    rm -rf /tmp/SoapyBladeRF && \
    ldconfig

# Signal Hound VSG60A (Soapy driver SignalHoundVSG60) — repo bundles libvsg_api + headers
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y libusb-1.0-0 && \
    rm -rf /var/lib/apt/lists/*
# Install bundled libvsg_api into ld path with SONAME libvsg_api.so.1 (required at runtime by libSignalHoundVSG60.so)
RUN git clone --depth 1 https://github.com/SignalHound/soapy-vsg.git /tmp/soapy-vsg && \
    VSG_SRC=$(ls /tmp/soapy-vsg/lib/libvsg_api.so.* | head -n1) && \
    cp -a "$VSG_SRC" /usr/local/lib/ && \
    VSG_BASE=$(basename "$VSG_SRC") && \
    ln -sf "$VSG_BASE" /usr/local/lib/libvsg_api.so.1 && \
    ln -sf libvsg_api.so.1 /usr/local/lib/libvsg_api.so && \
    cd /tmp/soapy-vsg/lib && ln -sf "$VSG_BASE" libvsg_api.so && \
    cd /tmp/soapy-vsg && mkdir build && cd build && \
    cmake .. && make -j"$(nproc)" && make install && \
    rm -rf /tmp/soapy-vsg && \
    ldconfig

# Pre-download bladeRF FPGA image so the entrypoint can load it at runtime
RUN mkdir -p /opt/bladerf && \
    wget -q https://www.nuand.com/fpga/hostedxA4-latest.rbf \
         -O /opt/bladerf/hostedxA4.rbf

# Without these lines the GNU Radio vmcircbuf backend fails to initialise
RUN mkdir -p /root/.gnuradio/prefs && \
    echo "vmcircbuf_default_factory=shmem" > /root/.gnuradio/prefs/vmcircbuf_default_factory
ENV HOME=/root
ENV PYTHONUNBUFFERED=1

# Copy source code into the container
WORKDIR /app
COPY pyproject.toml README.md /app/
COPY run_stream.py /app/
COPY src/ /app/src/
COPY entrypoint.sh /app/

# Install the python package
# GNU Radio from the Ubuntu PPA is compiled against NumPy 1.x;
# pip must not upgrade numpy beyond 1.x or gnuradio will fail to import.
# --ignore-installed is needed because some system distutils packages
# (blinker, etc.) can't be pip-uninstalled cleanly.
RUN python3 -m pip install --upgrade pip setuptools wheel
RUN python3 -m pip install --ignore-installed \
    "hubble-satnet-decoder>=1.1.1" \
    "numpy>=1.26,<2"
RUN python3 -m pip install --ignore-installed -e . "numpy>=1.26,<2"

# Start the live spectrogram + decoder web server
EXPOSE 8050
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "run_stream.py"]
