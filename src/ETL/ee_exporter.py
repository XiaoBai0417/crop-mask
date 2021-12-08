from datetime import date, datetime, timedelta
from pathlib import Path
from tqdm import tqdm
from typing import List, Optional, Tuple
from dataclasses import dataclass
from google.cloud import storage
import logging
import pandas as pd
import ee

from src.ETL.ee_boundingbox import BoundingBox, EEBoundingBox
from src.ETL import cloudfree
from src.ETL.constants import START, END, LAT, LON

logger = logging.getLogger(__name__)


def memoize(f):
    "Stores results of previous function to avoid re-calculating"
    memo = {}

    def helper(x="default"):
        if x not in memo:
            memo[x] = f() if x == "default" else f(x)
        return memo[x]

    return helper


@memoize
def get_cloud_tif_list(dest_bucket: str) -> List[str]:
    """Gets a list of all cloud-free TIFs in a bucket."""
    client = storage.Client()
    cloud_tif_list_iterator = client.list_blobs(dest_bucket, prefix="tifs")
    cloud_tif_list = [
        blob.name
        for blob in tqdm(cloud_tif_list_iterator, desc="Loading tifs already on Google Cloud")
    ]
    return cloud_tif_list


@memoize
def get_ee_task_list(key: str = "description") -> List[str]:
    """Gets a list of all active tasks in the EE task list."""
    task_list = ee.data.getTaskList()
    return [
        task[key]
        for task in tqdm(task_list, desc="Loading Earth Engine tasks")
        if task["state"] != "COMPLETED"
    ]


@dataclass
class EarthEngineExporter:
    r"""
    Setup parameters to download cloud free sentinel data for countries,
    where countries are defined by the simplified large scale
    international boundaries.
    :param days_per_timestep: The number of days of data to use for each mosaiced image.
    :param num_timesteps: The number of timesteps to export if season is not specified
    :param fast: Whether to use the faster cloudfree exporter. This function is considerably
        faster, but cloud artefacts can be more pronounced. Default = True
    :param monitor: Whether to monitor each task until it has been run
    :param credentials: The credentials to use for the export. If not specified, the default
    :param file_dimensions: The dimensions of the exported files.
    """
    days_per_timestep: int = 30
    num_timesteps: int = 12
    fast: bool = True
    monitor: bool = False
    credentials: Optional[str] = None
    file_dimensions: Optional[int] = None

    def check_earthengine_auth(self):
        try:
            if self.credentials:
                ee.Initialize(credentials=self.credentials)
            else:
                ee.Initialize()
        except Exception:
            logger.error(
                "This code doesn't work unless you have authenticated your earthengine account"
            )

    @staticmethod
    def cancel_all_tasks():
        ee.Initialize()
        tasks = ee.batch.Task.list()
        logger.info(f"Cancelling up to {len(tasks)} tasks")
        # Cancel running and ready tasks
        for task in tasks:
            task_id = task.status()["id"]
            task_state = task.status()["state"]
            if task_state == "RUNNING" or task_state == "READY":
                task.cancel()
                logger.info(f"Task {task_id} cancelled")
            else:
                logger.info(f"Task {task_id} state is {task_state}")

    def _export_for_polygon(
        self,
        polygon: ee.Geometry.Polygon,
        start_date: date,
        end_date: date,
        file_name_prefix: str,
        description: str,
        dest_bucket: Optional[str] = None,
    ):
        if end_date > datetime.now().date():
            raise ValueError(f"{end_date} is in the future")

        if self.fast:
            export_func = cloudfree.get_single_image_fast
        else:
            export_func = cloudfree.get_single_image

        image_collection_list: List[ee.Image] = []
        increment = timedelta(days=self.days_per_timestep)
        cur_date = start_date
        while (cur_date + increment) <= end_date:
            image_collection_list.append(
                export_func(region=polygon, start_date=cur_date, end_date=cur_date + increment)
            )
            cur_date += increment

        # now, we want to take our image collection and append the bands into a single image
        imcoll = ee.ImageCollection(image_collection_list)
        img = ee.Image(imcoll.iterate(cloudfree.combine_bands))

        cloudfree.export(
            image=img,
            region=polygon,
            dest_bucket=dest_bucket,
            file_name_prefix=file_name_prefix,
            monitor=self.monitor,
            file_dimensions=self.file_dimensions,
            description=description,
        )


@dataclass
class RegionExporter(EarthEngineExporter):
    def export(
        self,
        region_bbox: BoundingBox,
        dest_path: str,
        dest_bucket: Optional[str] = None,
        end_date: Optional[date] = None,
        start_date: Optional[date] = None,
        metres_per_polygon: Optional[int] = 10000,
    ) -> List[str]:
        r"""
        Run the regional exporter. For each label, the exporter will export
        data from (end_date - timedelta(days=days_per_timestep * num_timesteps)) to end_date
        where each timestep consists of a mosaic of all available images within the
        days_per_timestep of that timestep.
        :param region_bbox: The bounding box of the region to export
        :param dest_path: The folder path to export to
        :param dest_bucket: The name of the destination GCP bucket
        :param start_date: The start date of the data export
        :param end_date: The end date of the data export
        :param metres_per_polygon: Whether to split the export of a large region into smaller
            boxes of (max) area metres_per_polygon * metres_per_polygon. It is better to instead
            split the area once it has been exported
        """
        self.check_earthengine_auth()

        if start_date is None and isinstance(end_date, date):
            start_date = end_date - timedelta(days=self.days_per_timestep * self.num_timesteps)
        elif end_date is None and isinstance(start_date, date):
            end_date = start_date + timedelta(days=self.days_per_timestep * self.num_timesteps)

        if end_date is None or start_date is None:
            raise ValueError(
                "Unable to determine start_date, either 'season' or 'end_date' and "
                "'num_timesteps' must be set."
            )

        region = EEBoundingBox.from_bounding_box(region_bbox)
        general_identifier = f"{Path(dest_path).name}_{str(start_date)}_{str(end_date)}"
        if metres_per_polygon is not None:
            regions = region.to_polygons(metres_per_patch=metres_per_polygon)
            ids = [f"{i}_{general_identifier}" for i in range(len(regions))]
        else:
            regions = [region.to_ee_polygon()]
            ids = [general_identifier]

        for identifier, region in zip(ids, regions):
            self._export_for_polygon(
                polygon=region,
                file_name_prefix=f"{dest_path}/batch_{identifier}/{identifier}",
                description=identifier,
                start_date=start_date,
                end_date=end_date,
                dest_bucket=dest_bucket,
            )

        return ids


@dataclass
class LabelExporter(EarthEngineExporter):
    """
    Class for exporting tifs using labels
    :param dest_bucket: Destination bucket for tif files
    :param check_gcp: Whether to check Google Cloud Bucket before exporting
    :param check_ee: Whether to check Earth Engine before exporting
    :param surrounding_metres: The number of metres surrounding each labelled point to export
    """

    dest_bucket: str = "crop-mask-tifs"
    check_gcp: bool = True
    check_ee: bool = True
    surrounding_metres: int = 80

    def __post_init__(self):
        self.check_earthengine_auth()
        self.cloud_tif_list = get_cloud_tif_list(self.dest_bucket) if self.check_gcp else []
        self.ee_task_list = get_ee_task_list() if self.check_ee else []

    @staticmethod
    def _generate_filename_and_desc(
        bbox: BoundingBox, start_date: date, end_date: date
    ) -> Tuple[str, str]:
        """
        Generates filename for tif files that will be exported
        """
        min_lat = round(bbox.min_lat, 4)
        min_lon = round(bbox.min_lon, 4)
        max_lat = round(bbox.max_lat, 4)
        max_lon = round(bbox.max_lon, 4)
        filename = (
            f"min_lat={min_lat}_min_lon={min_lon}_max_lat={max_lat}_max_lon={max_lon}"
            + f"_dates={start_date}_{end_date}"
        )
        # Description of the export cannot contain certrain characters
        description = filename.replace(".", "-").replace("=", "-")[:100]

        return filename, description

    def _export_using_point_and_dates(
        self, lat: float, lon: float, start_date: date, end_date: date
    ):
        """
        Function to export tif around specified point for a specified date range
        """
        bbox = EEBoundingBox.from_centre(
            mid_lat=lat, mid_lon=lon, surrounding_metres=self.surrounding_metres
        )
        file_name_prefix, description = self._generate_filename_and_desc(bbox, start_date, end_date)

        if self.check_gcp and (f"tifs/{file_name_prefix}.tif" in self.cloud_tif_list):
            return False

        if self.check_ee and (description in self.ee_task_list):
            return True

        if self.check_ee and len(self.ee_task_list) >= 3000:
            return True

        self._export_for_polygon(
            file_name_prefix=f"tifs/{file_name_prefix}",
            description=description,
            dest_bucket=self.dest_bucket,
            polygon=bbox.to_ee_polygon(),
            start_date=start_date,
            end_date=end_date,
        )
        return True

    def export(self, labels: pd.DataFrame):
        r"""
        Run the exporter. For each label, the exporter will export
        int( (end_date - start_date).days / days_per_timestep) timesteps of data,
        where each timestep consists of a mosaic of all available images within the
        days_per_timestep of that timestep.
        """
        amount_exporting = 0
        amount_exported = 0
        for _, row in tqdm(labels.iterrows(), total=len(labels), desc="Exporting on GEE"):
            is_exporting = self._export_using_point_and_dates(
                lat=row[LAT],
                lon=row[LON],
                start_date=datetime.strptime(row[START], "%Y-%m-%d").date(),
                end_date=datetime.strptime(row[END], "%Y-%m-%d").date(),
            )
            if is_exporting:
                amount_exporting += 1
            else:
                amount_exported += 1

        if amount_exporting > 0:
            print(
                f"Exporting {amount_exporting} see progress: https://code.earthengine.google.com/"
            )
        if amount_exported > 0:
            print(
                f"{amount_exported} files exist on Google Cloud, run command to download:"
                + "\ngsutil -m cp -n -r gs://crop-mask-tifs/tifs data/"
            )
