from unittest import TestCase
from shapely.geometry import Point

import geopandas
import numpy as np
import pandas as pd

from utils import get_dvc_dir
from src.dataset_config import labeled_datasets
from src.ETL.dataset import LabeledDataset


class IntegrationTestLabels(TestCase):
    """Tests the processed labels script"""

    @classmethod
    def setUpClass(cls):
        test_dir = get_dvc_dir("test")
        cls.africa = geopandas.read_file(test_dir / "afr_g2014_2013_0")

    @staticmethod
    def create_points_df(d: LabeledDataset):
        df = geopandas.read_file(d.labels_path)
        points = [Point(xy) for xy in zip(df["lon"], df["lat"])]
        points_df = geopandas.GeoDataFrame(geometry=points)
        points_df["Source"] = d.dataset
        points_df["Country"] = d.country
        return points_df

    @staticmethod
    def within_country(point: Point, country: str):
        africa = IntegrationTestLabels.africa
        return point.within(africa[africa["ADM0_NAME"] == country]["geometry"].iloc[0])

    def test_all_processed_labels(self):
        """
        Currently this test merely prints out which labels are not witin the country boundary
        """
        get_dvc_dir("processed")
        all_dfs = [
            self.create_points_df(d) for d in labeled_datasets if d.labels_path.suffix == ".geojson"
        ]
        df = pd.concat(all_dfs)
        df["Within Country"] = np.vectorize(self.within_country)(df["geometry"], df["Country"])
        print("Labels NOT within country")
        print(df[df["Within Country"] == False]["Source"].value_counts())
        return df
