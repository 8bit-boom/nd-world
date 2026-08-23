# Alternative to the prebuilt ghcr.io/ggml-org/whisper.cpp:main image, for
# hosts whose CPU crashes it with "Illegal instruction" (SIGILL, container
# exit code 132) the moment a model finishes loading. That prebuilt image is
# compiled with ggml's GGML_NATIVE=ON default, which auto-detects and bakes
# in whatever instruction set the GitHub Actions build runner happens to
# support — including AVX-512 on many current runners — into the shipped
# binary. Any host CPU without AVX-512 (most consumer/prosumer hardware,
# including plenty of home NAS boxes, even ones with otherwise-modern
# AMD/Intel CPUs that support AVX2/FMA/F16C just fine) then hits an illegal
# instruction the instant whisper_model_load runs its first vectorized op.
# See https://github.com/ggml-org/whisper.cpp/issues/2928.
#
# Building with GGML_NATIVE=OFF instead makes ggml fall back to its portable
# baseline — SSE4.2/AVX/AVX2/FMA/F16C stay on, only AVX-512 (which defaults
# off regardless of GGML_NATIVE) is excluded — which every x86_64 CPU from
# roughly the last decade actually supports, at a small cost in raw speed
# versus a CPU-matched native build.
#
# Use this instead of the `image:` line on the "whisper" service — see the
# commented-out `build:` alternative there — only if you're hitting this
# exact crash; the prebuilt image is faster to pull and works fine on CPUs
# that do support AVX-512.
FROM ubuntu:22.04 AS build
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential cmake git ca-certificates && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
RUN git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git .
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF -DGGML_AVX512=OFF && \
    cmake --build build --config Release -j --target whisper-server

FROM ubuntu:22.04 AS runtime
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
COPY --from=build /app/build/bin/whisper-server /usr/local/bin/whisper-server
ENTRYPOINT ["whisper-server"]
