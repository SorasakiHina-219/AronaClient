# -*- coding: utf-8 -*-
# Copyright (C) 2024-2026 SorasakiHina-219
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Path utilities for AronaClient.

Provides shared path resolution logic for model files.
"""

import os
from pathlib import Path


def get_model_dir():
    """
    Get the model directory path.

    Returns:
        Path: The directory containing PMX model files.
              First checks ARONA_MODEL_DIR environment variable,
              then falls back to project's data/models directory.
    """
    env_path = os.getenv("ARONA_MODEL_DIR")
    if env_path:
        return Path(env_path)

    # Default: project root / data / models
    app_dir = Path(__file__).resolve().parent
    return app_dir / "data" / "models"


def get_pmx_path(model_name="Arona_1.01.pmx"):
    """
    Get the full path to a PMX model file.

    Args:
        model_name: Name of the PMX file (default: Arona_1.01.pmx)

    Returns:
        Path: Full path to the PMX file.
    """
    return get_model_dir() / model_name
