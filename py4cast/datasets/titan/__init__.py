import datetime as dt
import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Callable, Dict, List, Literal, Tuple, Union

import gif
import matplotlib.pyplot as plt
import numpy as np
import torch
import tqdm
import xarray as xr
from skimage.transform import resize
from torch.utils.data import DataLoader

from py4cast.datasets.base import (
    DatasetABC,
    DatasetInfo,
    Item,
    NamedTensor,
    Period,
    TorchDataloaderSettings,
    collate_fn,
    get_param_list,
)
from py4cast.datasets.titan.settings import (
    DEFAULT_CONFIG,
    FORMATSTR,
    METADATA,
    SCRATCH_PATH,
)
from py4cast.forcingutils import generate_toa_radiation_forcing, get_year_hour_forcing
from py4cast.plots import DomainInfo
from py4cast.utils import merge_dicts


class TitanAccessor(DataAccessor):

    def get_weight_per_level(
        level: int,
        level_type: Literal["isobaricInhPa", "heightAboveGround", "surface", "meanSea"],
    ):
        if level_type == "isobaricInhPa":
            return 1 + (level) / (1000)
        else:
            return 2.0

    #############################################################
    #                            GRID                           #
    #############################################################

    def load_grid_info(name: str) -> GridConfig:
        if name not in ["PAAROME_1S100", "PAAROME_1S40"]:
            raise NotImplementedError(
                "Grid must be in ['PAAROME_1S100', 'PAAROME_1S40']"
            )
        path = SCRATCH_PATH / f"conf_{name}.grib"
        conf_ds = xr.open_dataset(path)
        grid_info = METADATA["GRIDS"][name]
        full_size = grid_info["size"]
        landsea_mask = None
        grid_conf = GridConfig(
            full_size,
            conf_ds.latitude.values,
            conf_ds.longitude.values,
            conf_ds.h.values,
            landsea_mask,
        )
        return grid_conf

    def get_grid_coords(param: Param) -> List[int]:
        return METADATA["GRIDS"][param.grid.name]["extent"]

    #############################################################
    #                              PARAMS                       #
    #############################################################

    def load_param_info(name: str) -> ParamConfig:
        info = METADATA["WEATHER_PARAMS"][name]
        grib_name = info["grib"]
        grib_param = info["param"]
        unit = info["unit"]
        level_type = info["type_level"]
        long_name = info["long_name"]
        grid = info["grid"]
        if grid not in ["PAAROME_1S100", "PAAROME_1S40", "PA_01D"]:
            raise NotImplementedError(
                "Parameter native grid must be in ['PAAROME_1S100', 'PAAROME_1S40', 'PA_01D']"
            )
        return ParamConfig(unit, level_type, long_name, grid, grib_name, grib_param)

    #############################################################
    #                              LOADING                      #
    #############################################################

    def get_filepath(
        ds_name: str,
        param: Param,
        date: dt.datetime,
        file_format: Literal["npy", "grib"],
    ) -> Path:
        """
        Returns the path of the file containing the parameter data.
        - in grib format, data is grouped by level type.
        - in npy format, data is saved as npy, rescaled to the wanted grid, and each
        2D array is saved as one file to optimize IO during training."""
        if file_format == "grib":
            folder = SCRATCH_PATH / "grib" / date.strftime(FORMATSTR)
            return folder / param.grib_name
        else:
            npy_path = get_dataset_path(ds_name, param.grid) / "data"
            filename = f"{param.name}_{param.level}_{param.level_type}.npy"
            return npy_path / date.strftime(FORMATSTR) / filename

    def get_dataset_path(name: str, grid: Grid):
        str_subdomain = "-".join([str(i) for i in grid.subdomain])
        subdataset_name = f"{name}_{grid.name}_{str_subdomain}"
        return SCRATCH_PATH / "subdatasets" / subdataset_name

    def load_data_from_disk(
        ds_name: str,
        param: Param,
        date: dt.datetime,
        file_format: Literal["npy", "grib"] = "grib",
    ) -> np.ndarray:
        """
        Function to load invidiual parameter and lead time from a file stored in disk
        """
        data_path = get_filepath(ds_name, param, date, file_format)
        if file_format == "grib":
            arr, lons, lats = load_data_grib(param, data_path)
            arr = fit_to_grid(arr, lons, lats)
        else:
            arr = np.load(data_path)

        subdomain = param.grid.subdomain
        arr = arr[subdomain[0] : subdomain[1], subdomain[2] : subdomain[3]]
        if file_format == "grib":
            arr = arr[::-1]
        return arr  # invert latitude

    def get_param_tensor(
        param: Param,
        stats: Stats,
        dates: List[dt.datetime],
        settings: Settings,
        no_standardize: bool = False,
    ) -> torch.tensor:
        """
        Fetch data on disk fo the given parameter and all involved dates
        Unless specified, normalize the samples with parameter-specific constants
        returns a tensor
        """
        arrays = [
            load_data_from_disk(
                settings.dataset_name, param, date, settings.file_format
            )
            for date in dates
        ]
        arr = np.stack(arrays)
        # Extend dimension to match 3D (level dimension)
        if len(arr.shape) != 4:
            arr = np.expand_dims(arr, axis=1)
        arr = np.transpose(arr, axes=[0, 2, 3, 1])  # shape = (steps, lvl, x, y)
        if settings.standardize and not no_standardize:
            name = param.parameter_short_name
            means = np.asarray(stats[name]["mean"])
            std = np.asarray(stats[name]["std"])
            arr = (arr - means) / std
        return torch.from_numpy(arr)


############################################################
#                   HELPER FUNCTIONS for TITAN             #
############################################################


def fit_to_grid(
    param: Param,
    arr: np.ndarray,
    lons: np.ndarray,
    lats: np.ndarray,
    get_grid_coords: Callable[[Param], List[str]],
) -> np.ndarray:
    # already on good grid, nothing to do:
    if param.grid.name == param.native_grid:
        return arr

    # crop arpege data to arome domain:
    if param.native_grid == "PA_01D" and param.grid.name in [
        "PAAROME_1S100",
        "PAAROME_1S40",
    ]:
        grid_coords = get_grid_coords(param)
        # Mask ARPEGE data to AROME bounding box
        mask_lon = (lons >= grid_coords[2]) & (lons <= grid_coords[3])
        mask_lat = (lats >= grid_coords[1]) & (lats <= grid_coords[0])
        arr = arr[mask_lat, :][:, mask_lon]

    anti_aliasing = param.grid.name == "PAAROME_1S40"  # True if downsampling
    # upscale or downscale to grid resolution:
    return resize(arr, param.grid.full_size, anti_aliasing=anti_aliasing)


@lru_cache(maxsize=50)
def read_grib(path_grib: Path) -> xr.Dataset:
    return xr.load_dataset(path_grib, engine="cfgrib", backend_kwargs={"indexpath": ""})


def load_data_grib(param: Param, path: Path) -> np.ndarray:
    ds = read_grib(path)
    assert param.grib_param is not None
    level_type = ds[param.grib_param].attrs["GRIB_typeOfLevel"]
    lats = ds.latitude.values
    lons = ds.longitude.values
    if level_type != "isobaricInhPa":  # Only one level
        arr = ds[param.grib_param].values
    else:
        arr = ds[param.grib_param].sel(isobaricInhPa=param.level).values
    return arr, lons, lats


#############################################################
#                            DATASET                        #
#############################################################


class TitanDataset(DatasetABC):
    # Si on doit travailler avec plusieurs grilles, on fera un super dataset qui contient
    # plusieurs datasets chacun sur une seule grille
    def __init__(
        self,
        name: str,
        grid: Grid,
        period: Period,
        params: List[Param],
        settings: Settings,
    ):
        super().__init__(self, name, grid, period, params, settings, TitanAccessor)

    @cached_property
    def sample_list(self):
        """Creates the list of samples."""
        print("Start creating samples...")
        stats = self.stats if self.settings.standardize else None
        if self.valid_samples_file.exists():
            print(f"Retrieving valid samples from file {self.valid_samples_file}")
            with open(self.valid_samples_file, "r") as f:
                dates_str = [line[:-1] for line in f.readlines()]
                dateformat = "%Y-%m-%d_%Hh%M"
                dates = [dt.datetime.strptime(ds, dateformat) for ds in dates_str]
                dates = list(set(dates).intersection(set(self.period.date_list)))
                samples = [
                    Sample(date, self.settings, self.params, stats, self.grid)
                    for date in dates
                ]
        else:
            print(
                f"Valid samples file {self.valid_samples_file} does not exist. Computing samples list..."
            )
            samples = []
            for date in tqdm.tqdm(self.period.date_list):
                sample = Sample(date, self.settings, self.params, stats, self.grid)
                if sample.is_valid():
                    samples.append(sample)
        print(f"--> All {len(samples)} {self.period.name} samples are now defined")
        return samples
