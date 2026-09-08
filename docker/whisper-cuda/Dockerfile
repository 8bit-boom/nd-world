# CUDA-enabled whisper.cpp build targeting Volta (V100, compute capability
# 7.0 / sm_70) — the prebuilt ghcr.io/ggml-org/whisper.cpp:main-cuda image
# does NOT cover this card: its own upstream build compiles for
# CMAKE_CUDA_ARCHITECTURES='75;80;86;90' (Turing and newer only), which
# skips Volta entirely. docker-compose.yml's own comment previously
# recommended that image unconditionally for "NVIDIA GPU" — that guidance
# was wrong for a V100 specifically and has been corrected to point here
# instead (see docker-compose.gpu.yml and docs/GPU_SETUP.md §6).
#
# This is a source build for exactly that gap, mirroring docker/whisper/
# Dockerfile's own CPU build (same repo, same static-link reasoning) with
# GGML_CUDA turned on and CUDA_ARCHITECTURES pinned to 70. If this same
# image ever needs to also run well on a newer card, widen the
# semicolon-separated list (e.g. "70;75;80;86") — kept to just Volta's own
# architecture here since that's this deployment's only GPU, and every
# extra architecture roughly doubles nvcc's compile time for this target.
#
# CUDA 12.x still supports compiling for sm_70 — that support is
# deprecated starting in CUDA 13.0, which drops every pre-7.5 architecture
# outright (Maxwell/Pascal/Volta). Stay on a 12.x base image here, not
# 13.x, or this build fails immediately with "unsupported gpu architecture
# 'compute_70'".
FROM nvidia/cuda:12.6.1-devel-ubuntu22.04 AS build
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential cmake git ca-certificates && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
RUN git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git .
# BUILD_SHARED_LIBS=OFF: same reasoning as docker/whisper/Dockerfile's CPU
# build — a shared-lib build links whisper-server against
# libwhisper.so/libggml*.so at runtime, and those never make it into the
# runtime stage below without extra copying. This only affects whisper.cpp's
# OWN libraries — the CUDA runtime/cuBLAS libraries nvcc-compiled code
# depends on stay dynamically linked regardless, which is exactly why the
# runtime stage below is CUDA's own "-runtime" image (it ships those) and
# not a bare Ubuntu base.
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release \
      -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="70" \
      -DBUILD_SHARED_LIBS=OFF && \
    cmake --build build --config Release -j --target whisper-server

FROM nvidia/cuda:12.6.1-runtime-ubuntu22.04 AS runtime
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
COPY --from=build /app/build/bin/whisper-server /usr/local/bin/whisper-server
ENTRYPOINT ["whisper-server"]
