import torch
import numpy as np
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple, List
from pathlib import Path
import xarray as xr
import pandas as pd

from .data_classes import BoundingBox
from .constants import BANDS


def set_seed(seed: int = 42) -> None:
    np.random.seed(seed)
    torch.manual_seed(seed)
    random.seed(seed)


STR2BB = {
    "Kenya": BoundingBox(min_lon=33.501, max_lon=42.283, min_lat=-5.202, max_lat=6.002),
    "Busia": BoundingBox(
        min_lon=33.88389587402344,
        min_lat=-0.04119872691853491,
        max_lon=34.44007873535156,
        max_lat=0.7779454563313616,
    ),
    "NorthMalawi": BoundingBox(min_lon=32.688, max_lon=35.772, min_lat=-14.636, max_lat=-9.231),
    "SouthMalawi": BoundingBox(min_lon=34.211, max_lon=35.772, min_lat=-17.07, max_lat=-14.636),
    "Rwanda": BoundingBox(min_lon=28.841, max_lon=30.909, min_lat=-2.854, max_lat=-1.034),
    "Togo": BoundingBox(
        min_lon=-0.1501, max_lon=1.7779296875, min_lat=6.08940429687, max_lat=11.115625
    ),
}


def process_filename(
    filename: str, include_extended_filenames: bool
) -> Optional[Tuple[str, datetime, datetime]]:
    r"""
    Given an exported sentinel file, process it to get the start
    and end dates of the data. This assumes the filename ends with '.tif'
    """
    date_format = "%Y-%m-%d"

    identifier, start_date_str, end_date_str = filename[:-4].split("_")

    start_date = datetime.strptime(start_date_str, date_format)

    try:
        end_date = datetime.strptime(end_date_str, date_format)
        return identifier, start_date, end_date

    except ValueError:
        if include_extended_filenames:
            end_list = end_date_str.split("-")
            end_year, end_month, end_day = (
                end_list[0],
                end_list[1],
                end_list[2],
            )

            # if we allow extended filenames, we want to
            # differentiate them too
            id_number = end_list[3]
            identifier = f"{identifier}-{id_number}"

            return (
                identifier,
                start_date,
                datetime(int(end_year), int(end_month), int(end_day)),
            )
        else:
            print(f"Unexpected filename {filename} - skipping")
            return None


def load_tif(filepath: Path, start_date: datetime, days_per_timestep: int) -> xr.DataArray:
    r"""
    The sentinel files exported from google earth have all the timesteps
    concatenated together. This function loads a tif files and splits the
    timesteps
    """

    # this mirrors the eo-learn approach
    # also, we divide by 10,000, to remove the scaling factor
    # https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2
    da = xr.open_rasterio(filepath).rename("FEATURES") / 10000

    da_split_by_time: List[xr.DataArray] = []

    bands_per_timestep = len(BANDS)
    num_bands = len(da.band)

    assert (
        num_bands % bands_per_timestep == 0
    ), f"Total number of bands not divisible by the expected bands per timestep"

    cur_band = 0
    while cur_band + bands_per_timestep <= num_bands:
        time_specific_da = da.isel(band=slice(cur_band, cur_band + bands_per_timestep))
        time_specific_da["band"] = range(bands_per_timestep)
        da_split_by_time.append(time_specific_da)
        cur_band += bands_per_timestep

    timesteps = [
        start_date + timedelta(days=days_per_timestep) * i for i in range(len(da_split_by_time))
    ]

    combined = xr.concat(da_split_by_time, pd.Index(timesteps, name="time"))
    combined.attrs["band_descriptions"] = BANDS

    return combined
