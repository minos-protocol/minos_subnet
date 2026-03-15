# hap.py Docker Image

This document describes the custom `genonet/hap-py:0.3.15` Docker image used for VCF benchmarking in the validator scoring pipeline.

---

## Why a Custom Image?

The popular `mgibio/hap.py` Docker images have a **broken RTG Tools installation** that fails silently during vcfeval operations. This is a [known issue](https://github.com/Illumina/hap.py/issues/189) with no official fix.

The `genonet/hap-py` image solves the problem by:
1. Building hap.py 0.3.15 from source with Python 2.7 dependencies
2. Installing RTG Tools 3.12.1 separately (not the broken bundled version)
3. Pre-configuring RTG with `RTG_MEM=8g` to avoid Docker memory detection issues

---

## Quick Start

```bash
# Pull the image
docker pull genonet/hap-py:0.3.15

# Test hap.py
docker run --rm genonet/hap-py:0.3.15 --version

# Test RTG Tools
docker run --rm --entrypoint rtg genonet/hap-py:0.3.15 version
```

---

## Usage

### Run hap.py with vcfeval engine

```bash
docker run --rm \
  -v /path/to/data:/data \
  genonet/hap-py:0.3.15 \
  /data/truth.vcf.gz \
  /data/query.vcf.gz \
  -r /data/reference.fa \
  -o /data/output \
  --engine vcfeval \
  --engine-vcfeval-template /data/reference.sdf
```

### Run RTG vcfeval directly

```bash
docker run --rm --entrypoint rtg \
  -v /path/to/data:/data \
  genonet/hap-py:0.3.15 \
  vcfeval \
  -b /data/truth.vcf.gz \
  -c /data/query.vcf.gz \
  -t /data/reference.sdf \
  -o /data/output
```

---

## Components

| Component | Version | Source |
|-----------|---------|--------|
| hap.py | 0.3.15 | [Illumina/hap.py](https://github.com/Illumina/hap.py/releases/tag/v0.3.15) |
| RTG Tools | 3.12.1 | [RealTimeGenomics/rtg-tools](https://github.com/RealTimeGenomics/rtg-tools/releases/tag/3.12.1) |
| Base | Ubuntu 20.04 | Official Docker image |
| Java | OpenJDK 11 | Ubuntu package |

---

## Platform Notes

### Linux (AMD64)
Works natively, best performance.

### Mac (Apple Silicon M1/M2/M3)
Uses Rosetta 2 emulation automatically. Slightly slower but fully functional.

### Mac (Intel)
Works natively.

### Windows
Works with Docker Desktop (WSL2 backend recommended).

---

## Memory Configuration

RTG Tools is pre-configured with 8GB heap. Override if needed:

```bash
docker run --rm -e RTG_MEM=16g --entrypoint rtg genonet/hap-py:0.3.15 version
```

---

## Troubleshooting

### "Cannot determine system memory" warning
This is handled automatically via `RTG_MEM=8g` in the config. If you still see issues, set it explicitly:
```bash
docker run --rm -e RTG_MEM=4g ...
```

### Slow on Apple Silicon
This is expected due to x86 emulation. For heavy workloads, consider using a Linux AMD64 server.

### Permission denied errors
Mount volumes with proper permissions:
```bash
docker run --rm -u $(id -u):$(id -g) -v /path/to/data:/data ...
```

---

## Docker Hub

The image is available on Docker Hub at [hub.docker.com/r/genonet/hap-py](https://hub.docker.com/r/genonet/hap-py):

```bash
docker pull genonet/hap-py:0.3.15
```
