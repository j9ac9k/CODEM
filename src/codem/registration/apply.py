"""
ApplyRegistration.py
Project: CRREL-NEGGS University of Houston Collaboration
Date: February 2021

Applies the solved registration parameters to the original AOI data file.

This module contains the following class:

* ApplyRegistration: a class for applying registration results to the original
  unregistered AOI data file
"""
import json
import logging
import os
from typing import Optional
from typing import Tuple
from typing import Union

import codem.lib.resources as r
import numpy as np
import numpy.typing as npt
import pdal
import rasterio
import trimesh
from codem import __version__
from codem.preprocessing.preprocess import CodemParameters
from codem.preprocessing.preprocess import GeoData
from codem.preprocessing.preprocess import RegistrationParameters
from matplotlib.tri import LinearTriInterpolator
from matplotlib.tri import Triangulation
from numpy.lib import recfunctions as rfn


class ApplyRegistration:
    """
    A class to apply the solved registration to the original AOI data file.

    Parameters
    ----------
    fnd_obj: GeoData object
        The foundation data object
    aoi_obj: GeoData object
        The area of interest data object
    registration_parameters:
        Registration parameters from IcpRegistration
    residual_vectors: np.array
        Point to plane direction used in final ICP iteration
    residual_origins: np.array
        Origins of moving point used in final ICP iteration
    config: dict
        Dictionary of configuration options
    output_format: Optional[str]
        Provide file extension to be used for the output format

    Methods
    -------
    get_registration_transformation
    apply
    _apply_dsm
    _apply_mesh
    _apply_pointcloud
    _interpolate_residuals
    """

    def __init__(
        self,
        fnd_obj: GeoData,
        aoi_obj: GeoData,
        registration_parameters: RegistrationParameters,
        residual_vectors: npt.NDArray,
        residual_origins: npt.NDArray,
        config: CodemParameters,
        output_format: Optional[str],
    ) -> None:
        self.logger = logging.getLogger(__name__)
        self.fnd_crs = fnd_obj.crs
        self.fnd_units_factor = fnd_obj.units_factor
        self.fnd_units = fnd_obj.units
        self.aoi_file = aoi_obj.file
        self.aoi_nodata = aoi_obj.nodata
        self.aoi_resolution = aoi_obj.native_resolution
        self.aoi_crs = aoi_obj.crs
        self.aoi_units_factor = aoi_obj.units_factor
        self.aoi_type = aoi_obj.type
        self.aoi_area_or_point = aoi_obj.area_or_point
        self.registration_transform = registration_parameters["matrix"]
        self.registration_rmse = registration_parameters["rmse_3d"]
        self.residual_vectors = residual_vectors
        self.residual_origins = residual_origins
        self.config = config

        in_name = os.path.basename(self.aoi_file)
        root, ext = os.path.splitext(in_name)
        if output_format is not None:
            ext = (
                output_format if output_format.startswith(".") else f".{output_format}"
            )
        out_name = f"{root}_registered{ext}"
        self.out_name: str = os.path.join(self.config["OUTPUT_DIR"], out_name)

    def get_registration_transformation(
        self,
    ) -> Union[np.ndarray, pdal.Pipeline]:
        """
        Generates the transformation from the AOI to FND coordinate system.
        The transformation accommodates linear unit differences and the solved
        registration matrix, which is only valid for linear units of meters.

        Returns
        --------
        registration_transformation:
            np.ndarray : Registration matrix
            dict     : PDAL filters.transformation stage with SRS overide if available
        """
        aoi_to_meters = np.eye(4) * self.aoi_units_factor
        aoi_to_meters[3, 3] = 1
        meters_to_fnd = np.eye(4) * (1 / self.fnd_units_factor)
        meters_to_fnd[3, 3] = 1

        aoi_to_fnd_array: npt.NDArray = (
            meters_to_fnd @ self.registration_transform @ aoi_to_meters
        )

        if self.aoi_type == "mesh":
            return aoi_to_fnd_array
        else:
            aoi_to_fnd_array = np.reshape(aoi_to_fnd_array, (1, 16))
            aoi_to_fnd_string = [
                " ".join(item) for item in aoi_to_fnd_array.astype(str)
            ][0]
            registration_transformation = pdal.Filter.transformation(
                matrix=aoi_to_fnd_string
            )
            return registration_transformation

    def apply(self) -> None:
        """
        Call the appropriate registration function depending on data type
        """
        if os.path.splitext(self.aoi_file)[-1] in r.dsm_filetypes:
            self._apply_dsm()
        if os.path.splitext(self.aoi_file)[-1] in r.mesh_filetypes:
            self._apply_mesh()
        if os.path.splitext(self.aoi_file)[-1] in r.pcloud_filetypes:
            self._apply_pointcloud()

    def _apply_dsm(self) -> None:
        """
        Applies the registration transformation to a dsm file.
        We do not simply edit the transform of the DSM file because that is
        generally used to express 2D information. Instead, we apply the solved
        3D transformation to the 2.5D data and "re-raster" it.
        """
        input_name = os.path.basename(self.aoi_file)
        root, ext = os.path.splitext(input_name)
        output_name = f"{root}_registered{ext}"
        output_path = os.path.join(self.config["OUTPUT_DIR"], output_name)
        # construct pdal pipeline
        pipeline = pdal.Reader.gdal(
            filename=self.aoi_file,
            header="Z",
        )

        # no nodata is present, filter based on its limits
        if self.aoi_nodata is not None:
            pipeline |= pdal.Filter.range(
                limits=f"Z![{self.aoi_nodata}:{self.aoi_nodata}]"
            )

        # handle the case where the AOI underwent a CRS change
        if self.aoi_crs is not None:  # if statement to satisfy mypy
            pipeline |= pdal.Filter.reprojection(out_srs=self.aoi_crs.to_wkt())

        # insert the transform filter to register the AOI
        registration_task = self.get_registration_transformation()
        if isinstance(registration_task, pdal.pipeline.Filter):
            pipeline |= registration_task
        else:
            raise ValueError(
                f"get_registration_transformation returned {type(registration_task)} "
                "not a dictionary of strings as needed for the pdal pipeline."
            )

        writer_kwargs = {
            "resolution": self.aoi_resolution,
            "output_type": "idw",
            "filename": output_path,
            "metadata": (
                f"CODEM_VERSION={__version__},"
                "CODEM_INFO=Data registered and adjusted to "
                f"{os.path.basename(self.config['FND_FILE'])} by NCALM CODEM. "
                f"Total registration mean square error {self.registration_rmse:.3f},"
                "TIFFTAG_IMAGEDESCRIPTION=RegisteredCompliment"
            ),
        }

        if self.aoi_area_or_point in ("Area", "Point"):
            writer_kwargs["metadata"] += f",AREA_OR_POINT={self.aoi_area_or_point}"  # type: ignore

        if self.aoi_nodata is not None:
            writer_kwargs["nodata"] = self.aoi_nodata

        pipeline |= pdal.Writer.gdal(**writer_kwargs)
        pipeline.execute()

        self.logger.info(
            f"Registration has been applied to AOI-DSM and saved to: {self.out_name}"
        )
        if self.config["ICP_SAVE_RESIDUALS"]:
            with rasterio.open(self.out_name) as src:
                dsm = src.read(1)
                transform = src.transform
                nodata = src.nodata
                tags = src.tags()
                if "AREA_OR_POINT" in tags and tags["AREA_OR_POINT"] == "Area":
                    area_or_point = "Area"
                elif "AREA_OR_POINT" in tags and tags["AREA_OR_POINT"] == "Point":
                    area_or_point = "Point"
                else:
                    area_or_point = "Area"
                profile = src.profile

            rows = np.arange(dsm.shape[0], dtype=np.float64)
            cols = np.arange(dsm.shape[1], dtype=np.float64)
            uu, vv = np.meshgrid(cols, rows)
            u: npt.NDArray = np.reshape(uu, -1)
            v: npt.NDArray = np.reshape(vv, -1)
            if area_or_point == "Area":
                u += 0.5
                v += 0.5
            xy = np.asarray(transform * (u, v)).T

            nan_mask = np.isnan(dsm)
            if nodata is not None:
                dsm[nan_mask] = nodata
                mask = dsm == nodata
            else:
                mask = nan_mask
            mask = np.reshape(mask, -1)

            # interpolate the residual grid for each xy
            res_x, res_y, res_z, res_horiz, res_3d = self._interpolate_residuals(
                xy[:, 0], xy[:, 1]
            )

            res_x[mask] = nodata
            res_y[mask] = nodata
            res_z[mask] = nodata
            res_horiz[mask] = nodata
            res_3d[mask] = nodata

            res_x = np.reshape(res_x, dsm.shape)
            res_y = np.reshape(res_y, dsm.shape)
            res_z = np.reshape(res_z, dsm.shape)
            res_horiz = np.reshape(res_horiz, dsm.shape)
            res_3d = np.reshape(res_3d, dsm.shape)

            # save the interpolated data to a new TIF file. We only save to TIF
            # files since they are known to handle additional bands.
            root, _ = os.path.splitext(self.out_name)
            out_name_res = f"{root}_residuals.tif"

            profile.update(count=6, driver="GTiff")

            with rasterio.open(out_name_res, "w", **profile) as dst:
                dst.write(dsm, 1)
                dst.write(res_x, 2)
                dst.write(res_y, 3)
                dst.write(res_z, 4)
                dst.write(res_horiz, 5)
                dst.write(res_3d, 6)
                dst.set_band_description(1, "DSM")
                dst.set_band_description(2, "ResidualX")
                dst.set_band_description(3, "ResidualY")
                dst.set_band_description(4, "ResidualZ")
                dst.set_band_description(5, "ResidualHoriz")
                dst.set_band_description(6, "Residual3D")

            self.logger.info(
                f"ICP residuals have been computed for each registered AOI-DSM cell and saved to: {out_name_res}"
            )

    def _apply_mesh(self) -> None:
        """
        Applies the registration transformation to a mesh file. No attempt is
        made to write the coordinate reference system since mesh files typically
        do not store coordinate reference system information.
        """
        mesh = trimesh.load_mesh(self.aoi_file)

        mesh.apply_transform(self.get_registration_transformation())
        mesh.units = self.fnd_units

        root, ext = os.path.splitext(self.aoi_file)

        if ext == ".obj":
            base_name = os.path.basename(root)
            mesh.visual.material.name = base_name

        mesh.export(self.out_name)
        self.logger.info(
            f"Registration has been applied to AOI-MESH and saved to: {self.out_name}"
        )

        if self.config["ICP_SAVE_RESIDUALS"]:
            registered_mesh = trimesh.load_mesh(self.out_name)
            vertices = registered_mesh.vertices
            x = vertices[:, 0]
            y = vertices[:, 1]

            # interpolate the residual grid for each xy
            res_x, res_y, res_z, res_horiz, res_3d = self._interpolate_residuals(x, y)

            # save the interpolated data to a new PLY file. We only save to PLY
            # files since they are known to handle additional vertex attributes.
            attributes = dict(
                {
                    "ResidualX": res_x,
                    "ResidualY": res_y,
                    "ResidualZ": res_z,
                    "ResidualHoriz": res_horiz,
                    "Residual3D": res_3d,
                }
            )
            registered_mesh.vertex_attributes = attributes

            root, _ = os.path.splitext(self.out_name)
            out_name_res = root + "_residuals.ply"
            registered_mesh.export(out_name_res)

            self.logger.info(
                f"ICP residuals have been computed for each registered AOI-MESH vertex and saved to: {out_name_res}"
            )

    def _apply_pointcloud(self) -> None:
        """
        Applies the registration transformation to a point cloud file.
        """
        pipeline = pdal.Reader(self.aoi_file)
        pipeline |= self.get_registration_transformation()

        writer_kwargs = {"filename": self.out_name}
        if self.fnd_crs is not None:
            writer_kwargs["a_srs"] = self.fnd_crs.to_wkt()
        writer_kwargs["forward"] = "all"
        writer_kwargs["offset_x"] = self.config["OFFSET_X"]
        writer_kwargs["offset_y"] = self.config["OFFSET_Y"]
        writer_kwargs['offset_z'] = self.config["OFFSET_Z"]
        writer_kwargs["scale_x"] = self.config["SCALE_X"]
        writer_kwargs["scale_y"] = self.config["SCALE_Y"]
        writer_kwargs['scale_z'] = self.config["SCALE_Z"]
        pipeline |= pdal.Writer.las(**writer_kwargs)

        pipeline.execute()
        self.logger.info(
            f"Registration has been applied to AOI-PCLOUD and saved to: {self.out_name}"
        )

        if self.config["ICP_SAVE_RESIDUALS"]:
            # open up the registered output file, read in xy's
            p = pdal.Reader(self.out_name).pipeline()
            p.execute()
            arrays = p.arrays
            array = arrays[0]
            x = array["X"]
            y = array["Y"]

            # interpolate the residual grid for each xy
            res_x, res_y, res_z, res_horiz, res_3d = self._interpolate_residuals(x, y)

            # save the interpolated residuals to a new LAZ file. We only save
            # to LAS version 1.4 files since they are known to handle additional
            # point dimensions (attributes)
            res_dtype = np.dtype(
                [
                    ("ResidualX", np.double),
                    ("ResidualY", np.double),
                    ("ResidualZ", np.double),
                    ("ResidualHoriz", np.double),
                    ("Residual3D", np.double),
                ]
            )
            res_data = np.zeros(array.shape[0], dtype=res_dtype)
            res_data["ResidualX"] = res_x
            res_data["ResidualY"] = res_y
            res_data["ResidualZ"] = res_z
            res_data["ResidualHoriz"] = res_horiz
            res_data["Residual3D"] = res_3d

            original_and_res = rfn.merge_arrays((array, res_data), flatten=True)

            root, _ = os.path.splitext(self.out_name)
            out_name_res = root + "_residuals.laz"
            pipe = [
                {
                    "type": "writers.las",
                    "minor_version": 4,
                    "extra_dims": "all",
                    "filename": out_name_res,
                }
            ]
            p = pdal.Pipeline(
                json.dumps(pipe),
                arrays=[
                    original_and_res,
                ],
            )
            p.execute()

            self.logger.info(
                f"ICP residuals have been computed for each registered AOI-PCLOUD point and saved to: {out_name_res}"
            )

    def _interpolate_residuals(
        self, x: npt.NDArray, y: npt.NDArray
    ) -> Tuple[npt.NDArray, npt.NDArray, npt.NDArray, npt.NDArray, npt.NDArray]:
        """
        Interpolate ICP residuals at registered AOI x,y locations. The
        registration is solved using a gridded set of points, while the AOI
        x,y locations may be disorganized and/or at a different resolution
        than the registration grid. Therefore, we interpolate.
        """
        # We need to scale the residual origins and vectors to the Foundation
        # linear unit, which the registered AOI data has been converted to as
        # part of the registration. Recall that the pipeline always runs in
        # meters, but the Foundation may have a different linear unit.
        meters_to_fnd = np.eye(4) * (1 / self.fnd_units_factor)
        meters_to_fnd[3, 3] = 1

        meters_res_origins = self.residual_origins
        meters_res_origins = np.hstack(
            (meters_res_origins, np.ones((meters_res_origins.shape[0], 1)))
        )
        fnd_res_origins = (meters_to_fnd @ meters_res_origins.T).T
        fnd_res_origins = fnd_res_origins[:, 0:3]

        meters_res_vectors = self.residual_vectors
        meters_res_vectors = np.hstack(
            (meters_res_vectors, np.ones((meters_res_vectors.shape[0], 1)))
        )
        fnd_res_vectors = (meters_to_fnd @ meters_res_vectors.T).T
        fnd_res_vectors = fnd_res_vectors[:, 0:3]

        # We want to store residual components and combined representations
        x_res = fnd_res_vectors[:, 0]
        y_res = fnd_res_vectors[:, 1]
        z_res = fnd_res_vectors[:, 2]
        horiz_res = np.sqrt(x_res**2 + y_res**2)
        threeD_res = np.sqrt(np.sum(fnd_res_vectors**2, axis=1))

        # Nearest neighbor is faster, but a linear interpolation looks better
        # Replace any NaN values produced by the interpolator with an obviously
        # incorrect value (-9999)
        triFn = Triangulation(fnd_res_origins[:, 0], fnd_res_origins[:, 1])

        linTriFn = LinearTriInterpolator(triFn, x_res)
        interp_res_x = linTriFn(x, y)
        interp_res_x[np.isnan(interp_res_x)] = -9999.0

        linTriFn = LinearTriInterpolator(triFn, y_res)
        interp_res_y = linTriFn(x, y)
        interp_res_y[np.isnan(interp_res_y)] = -9999.0

        linTriFn = LinearTriInterpolator(triFn, z_res)
        interp_res_z = linTriFn(x, y)
        interp_res_z[np.isnan(interp_res_z)] = -9999.0

        linTriFn = LinearTriInterpolator(triFn, horiz_res)
        interp_res_horiz = linTriFn(x, y)
        interp_res_horiz[np.isnan(interp_res_horiz)] = -9999.0

        linTriFn = LinearTriInterpolator(triFn, threeD_res)
        interp_res_3d = linTriFn(x, y)
        interp_res_3d[np.isnan(interp_res_3d)] = -9999.0

        return interp_res_x, interp_res_y, interp_res_z, interp_res_horiz, interp_res_3d
