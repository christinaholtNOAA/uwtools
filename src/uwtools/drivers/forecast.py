"""
Drivers for forecast models.
"""


import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Dict

from uwtools.config.core import FieldTableConfig, NMLConfig, YAMLConfig
from uwtools.drivers.driver import Driver
from uwtools.scheduler import BatchScript
from uwtools.types import Optional, OptionalPath
from uwtools.utils.file import handle_existing, readable, change_dir

class Forecast(Driver):

    def __init__(
        self,
        config_file: str,
        dry_run: bool = False,
        batch_script: Optional[str] = None,
    ):
        """
        Initialize the Forecast Driver.
        """

        super().__init__(config_file=config_file, dry_run=dry_run, batch_script=batch_script)

    # Public methods

    def batch_script(self) -> BatchScript:
        """
        Prepare batch script contents for interaction with system scheduler.
        """
        pre_run = self._mpi_env_variables("\n")
        bs = self.scheduler.batch_script
        bs.append(pre_run)
        bs.append(self.run_cmd())
        return bs

    def create_directory_structure(self, run_directory: DefinitePath, exist_act="delete"):
        """
        Creates the run directory for the forecast
        """
        self._create_run_directory(run_directory, exist_act)

    def create_namelist(self, output_path: OptionalPath) -> None:
        """
        Uses an object with user supplied values and an optional namelist base file to create an
        output namelist file. Will "dereference" the base file.

        :param output_path: Optional location of output namelist.
        """
        self._create_user_updated_config(
            config_class=NMLConfig,
            config_values=self._config["namelist"],
            output_path=output_path,
        )

    def create_streams(self, output_path: OptionalPath) -> None:

        template_file = self._config["streams"]["template"]
        values = self._config["streams"]["vars"]

        with readable(template_file) as f:
            template_str = f.read()

        template = J2Template(values=values, template_str=template_str) 
        with writeable(output_path) as f:
            print(template.render(), file=f)

    def output(self) -> None:
        """
        ???
        """

    def requirements(self) -> None:
        """
        ???
        """

    def resources(self) -> Mapping:
        """
        Parses the config and returns a formatted dictionary for the batch script.
        """

        return {
            "account": self._experiment_config["user"]["account"],
            "scheduler": self._experiment_config["platform"]["scheduler"],
            **self._config["jobinfo"],
        }

    def run(self, cycle: datetime) -> None:
        """
        Runs FV3 either as a subprocess or by submitting a batch script.
        """
        run_dir = Path(self._config["run_dir"].format(cycle=self._cycle.strfmt("%Y%d%m%H")))


        # Prepare directories.
        self.create_directory_structure(run_dir.as_posix(), "delete")

        if self._config.get("need_boundary_files", False):
            self._config["cycle-dependent"].update(self._define_boundary_files())

        for file_category in ["static", "cycle-dependent"]:
            self.stage_files(run_dir, self._config.get(file_category, {}), link_files=True)

        self._prepare_config_files()

        if self._batch_script is not None:
            batch_script = self.batch_script

            if self._dry_run:
                # Apply switch to allow user to view the run command of config.
                # This will not run the job.
                logging.info("Batch Script:")
                logging.info(batch_script)
                return

            outpath = run_dir / self._batch_script
            BatchScript.dump(str(batch_script), outpath)
            self.scheduler.run_job(outpath)
            return

        if self._dry_run:
            logging.info("Would run: ")
            logging.info(self.run_cmd())
            return

        with change_dir(run_dir):
            subprocess.run(
                f"{self.run_cmd()}",
                stderr=subprocess.STDOUT,
                check=False,
                shell=True,
            )

    @property
    def _config(self) -> Mapping:
        """
        The config object that describes the subset of an experiment config related to the
        FV3Forecast.
        """
        return self._experiment_config["forecast"]

    def _mpi_env_variables(self, delimiter=" ") -> str:
        """
        Returns a bash string of environment variables needed to run the MPI job.
        """
        return delimiter.join([f"{k}={v}" for k, v in self._config.get("mpi_settings", {}).items()])


class FV3Forecast(Forecast):
    """
    A driver for the FV3 forecast model.
    """

    def __init__(
        self,
        config_file: str,
        dry_run: bool = False,
        batch_script: Optional[str] = None,
    ):
        """
        Initialize the Forecast Driver.
        """

        super().__init__(config_file=config_file, dry_run=dry_run, batch_script=batch_script)

    # Public methods

    def create_directory_structure(self, run_directory: DefinitePath, exist_act="delete"):
        """
        Collects the name of the desired run directory, and has an optional flag for what to do if
        the run directory specified already exists. Creates the run directory and adds
        subdirectories INPUT and RESTART. Verifies creation of all directories.

        :param run_directory: Path of desired run directory.
        :param exist_act: Could be any of 'delete', 'rename', 'quit'. Sets how the program responds
            to a preexisting run directory. The default is to delete the old run directory.
        """

        self._create_run_directory(run_directory, exist_act=exist_act)
        # Create new run directory with two required subdirectories.
        for subdir in ("INPUT", "RESTART"):
            path = os.path.join(run_directory, subdir)
            logging.info("Creating directory: %s", path)
            os.makedirs(path)

    def create_field_table(self, output_path: OptionalPath) -> None:
        """
        Uses the forecast config object to create a Field Table.

        :param output_path: Optional location of output field table.
        """
        self._create_user_updated_config(
            config_class=FieldTableConfig,
            config_values=self._config["field_table"],
            output_path=output_path,
        )

    def create_model_configure(self, output_path: OptionalPath) -> None:
        """
        Uses the forecast config object to create a model_configure.

        :param output_path: Optional location of the output model_configure file.
        """
        self._create_user_updated_config(
            config_class=YAMLConfig,
            config_values=self._config["model_configure"],
            output_path=output_path,
        )


    @property
    def schema_file(self) -> str:
        """
        The path to the file containing the schema to validate the config file against.
        """
        with resources.as_file(resources.files("uwtools.resources")) as path:
            return (path / "FV3Forecast.jsonschema").as_posix()

    # Private methods

    def _boundary_hours(self, lbcs_config: Dict) -> tuple[int, int, int]:
        offset = abs(lbcs_config["offset"])
        end_hour = self._config["length"] + offset + 1
        return offset, lbcs_config["interval_hours"], end_hour

    def _define_boundary_files(self) -> Dict:
        """
        Maps the prepared boundary conditions to the appropriate hours for the forecast.
        """
        boundary_files = {}
        lbcs_config = self._experiment_config["preprocessing"]["lateral_boundary_conditions"]
        boudary_file_template = lbcs_config["output_file_template"]
        offset, interval, endhour = self._boundary_hours(lbcs_config)
        for tile in self._config["tiles"]:
            for boundary_hour in range(offset, endhour, interval):
                forecast_hour = boundary_hour - offset
                link_name = f"gfs_bndy.tile{tile}.{forecast_hour}.nc"
                boundary_file_path = boudary_file_template.format(
                    tile=tile,
                    forecast_hour=boundary_hour,
                )
                boundary_files.update({link_name: boundary_file_path})

        return boundary_files

    def _mpi_env_variables(self, delimiter=" "):
        """
        Returns a bash string of environment variables needed to run the MPI job.
        """
        envvars = {
            "KMP_AFFINITY": "scatter",
            "OMP_NUM_THREADS": self._config["runtime_info"].get("threads", 1),
            "OMP_STACKSIZE": self._config["runtime_info"].get("threads", "512m"),
            "MPI_TYPE_DEPTH": 20,
            "ESMF_RUNTIME_COMPLIANCECHECK": "OFF:depth=4",
        }
        return delimiter.join([f"{k}={v}" for k, v in envvars.items()])

    def _prepare_config_files(self, run_directory: DefinitePath) -> None:
        """
        Collect all the configuration files needed for FV3
        """

        self.create_field_table(run_directory / "field_table")
        self.create_model_configure(run_direcotory / "model_configure")
        self.create_namelist(run_directory / "input.nml")


class MPASForecast(Forecast):

    """
    A Driver for the MPAS Atmosphere forecast model.
    """

    def __init__(
        self,
        config_file: str,
        dry_run: bool = False,
        batch_script: Optional[str] = None,
    ):
        """
        Initialize the Forecast Driver.
        """

        super().__init__(config_file=config_file, dry_run=dry_run, batch_script=batch_script)

    def _prepare_config_files(self, run_directory: DefinitePath) -> None:
        """
        Collect all the configuration files needed for FV3
        """
        self.create_streams(run_directory / "streams.atmosphere")
        self.create_namelist(run_directory / "namelist.atmosphere")


class MPASInit(Forecast):

    """
    A Driver for the MPAS Atmosphere forecast model.
    """

    def __init__(
        self,
        config_file: str,
        dry_run: bool = False,
        batch_script: Optional[str] = None,
    ):
        """
        Initialize the Forecast Driver.
        """

        super().__init__(config_file=config_file, dry_run=dry_run, batch_script=batch_script)

    @property
    def _config(self) -> Mapping:
        """
        The config object that describes the subset of an experiment config related to the
        MPAS Init.
        """
        return self._experiment_config["preprocessing"]

    def _prepare_config_files(self, run_directory: DefinitePath) -> None:
        """
        Collect all the configuration files needed for FV3
        """
        self.create_streams(run_directory / "streams.init_atmosphere")
        self.create_namelist(run_directory / "namelist.init_atmosphere")



class Ungrib(Forecast):

    """
    A Driver for ungrib.
    """

    def __init__(
        self,
        config_file: str,
        dry_run: bool = False,
        batch_script: Optional[str] = None,
    ):
        """
        Initialize the Forecast Driver.
        """

        super().__init__(config_file=config_file, dry_run=dry_run, batch_script=batch_script)

    def run_cmd(self, *args) -> str:
        exec_name = self._config["exec_name"]
        return f"./{exec_name}"

    @property
    def _config(self) -> Mapping:
        """
        The config object that describes the subset of an experiment config related to the
        MPAS Init.
        """
        return self._experiment_config["prepare_ics"]

    def _prepare_config_files(self, run_directory: DefinitePath) -> None:
        """
        Collect all the configuration files needed for FV3
        """
        self.create_namelist(run_directory / "namelist.init_atmosphere")


    

CLASSES = {
    "FV3": FV3Forecast,
    "MPAS": MPASForecast,
    }
