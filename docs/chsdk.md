# ComputeHorde SDK runner

Minos defaults to `CONTAINER_RUNNER=native`. To opt in,
install the CHSDK below, then set this single setting in the validator's `.env`:

```dotenv
CONTAINER_RUNNER=chsdk
```

## Installation

Install `compute-horde-sdk` from PyPI together with the Minos stack.

```bash
python -m pip install -r requirements-chsdk.txt
python -m pip check
```

## Testing

Install `requirements-dev.txt` into the selected environment, then run:

```bash
python -m pytest
MINOS_CHSDK_DOCKER_TESTS=1 python -m pytest tests/test_chsdk_docker.py
```
