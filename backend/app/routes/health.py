"""Health check route — confirms the server is up AND the GPU is reachable."""
import sys

import torch
from fastapi import APIRouter

from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    cuda_ok = torch.cuda.is_available()
    return HealthResponse(
        status="ok",
        python_version=sys.version.split()[0],
        cuda_available=cuda_ok,
        gpu_name=torch.cuda.get_device_name(0) if cuda_ok else None,
        vram_total_mb=(
            torch.cuda.get_device_properties(0).total_memory // 1024 // 1024
            if cuda_ok
            else None
        ),
    )
