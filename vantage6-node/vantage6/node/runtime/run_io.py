"""
Preparation of the files and environment variables that are passed to an
algorithm run.

The files (input, output and token) and the environment variables that point to
them form the contract between the node and the algorithm. This contract is the same
for every container runtime, so that the same algorithm image can be used on nodes
that use different runtimes.
"""

import base64
import json
import logging
import os
from pathlib import Path

from vantage6.common.globals import ENV_VAR_EQUALS_REPLACEMENT, STRING_ENCODING
from vantage6.common.task_status import TaskStatus
from vantage6.node.docker.exceptions import PermanentAlgorithmStartFail
from vantage6.node.globals import ENV_VARS_NOT_SETTABLE_BY_NODE


class RunIOMixin:
    """
    Prepares the task folder, the input/output/token files and the environment
    variables of an algorithm run.

    Classes using this mixin should set the attributes ``run_id``, ``task_id``,
    ``node_id``, ``collaboration_id``, ``databases``, ``write_run_context_file``,
    ``status``, ``log``, ``_tasks_dir`` (folder on the node in which the task folders
    are created), ``data_folder`` (location of ``_tasks_dir`` in the algorithm
    container) and ``tmp_folder`` (location of the temporary folder in the
    algorithm container).
    """

    log: logging.Logger
    run_id: int
    task_id: int
    node_id: int
    collaboration_id: int
    databases: dict
    write_run_context_file: bool
    status: TaskStatus
    _tasks_dir: Path
    data_folder: str
    tmp_folder: str

    # File permissions applied to the task folder and the files in it. If None,
    # the defaults of the node process are used.
    task_folder_mode: int | None = None
    task_file_mode: int | None = None

    @staticmethod
    def _printable_input(input_: str | dict) -> str:
        """
        Return a version of the input with limited number of characters

        Parameters
        ----------
        input: str | dict
            Deserialized input of a task

        Returns
        -------
        str
            Input with limited number of characters, to be printed to logs
        """
        if isinstance(input_, dict):
            input_ = str(input_)
        if len(input_) > 550:
            return f"{input_[:500]}... ({len(input_)-500} characters omitted)"
        return input_

    def _make_task_folders(self) -> None:
        """Generate task folders"""
        # FIXME: We should have a separate mount/volume for this. At the
        #   moment this is a potential leak as containers might access input,
        #   output and token from other containers.
        #
        #   This was not possible yet as mounting volumes from containers
        #   is terrible when working from windows (as you have to convert
        #   from windows to unix several times...).

        # If we're running in docker _tasks_dir will point to a location on
        # the data volume.
        # Alternatively, if we're not running in docker it should point to the
        # folder on the host that can act like a data volume. In both cases,
        # we can just copy the required files to it
        self.task_folder_name = f"task-{self.run_id:09d}"
        self.task_folder_path = os.path.join(self._tasks_dir, self.task_folder_name)
        os.makedirs(self.task_folder_path, exist_ok=True)
        if self.task_folder_mode is not None:
            os.chmod(self.task_folder_path, self.task_folder_mode)
        self.output_file = os.path.join(self.task_folder_path, "output")

    def _write_io_files(self, docker_input: bytes | str, token: str) -> None:
        """
        Write the input, output and token files of the algorithm run

        Parameters
        ----------
        docker_input: bytes | str
            Input that can be read by the algorithm
        token: str
            Bearer token that the algorithm can use
        """
        if isinstance(docker_input, str):
            docker_input = docker_input.encode("utf8")
        self.docker_input = docker_input

        # Create I/O files & token for the algorithm container
        self.log.debug("prepare IO files in task folder")
        io_files = [
            ("input", self.docker_input),
            ("output", b""),
            ("token", token.encode("ascii")),
        ]

        for filename, data in io_files:
            filepath = os.path.join(self.task_folder_path, filename)

            with open(filepath, "wb") as fp:
                fp.write(data)
            if self.task_file_mode is not None:
                os.chmod(filepath, self.task_file_mode)

    def _prepare_environment(
        self, algorithm_env: dict, databases_to_use: list[dict]
    ) -> dict:
        """
        Write the run context file (if enabled) and set up the environment
        variables of the algorithm run

        Parameters
        ----------
        algorithm_env: dict
            Dictionary with additional environment variables to set
        databases_to_use: list[dict]
            List of database selection objects to use in the task

        Returns
        -------
        dict
            Environment variables of the algorithm run
        """
        proxy_host = self._get_proxy_host()
        if self.write_run_context_file:
            # create run context file for algorithm run
            # `method`, `args` and `kwargs` live in docker/run_input
            deserialized_input = self._deserialize_run_input(self.docker_input)
            self._write_run_context_file(
                input_data=deserialized_input,
                databases_to_use=databases_to_use,
                proxy_host=proxy_host,
            )

        # setup environment variables
        return self._setup_environment_vars(
            algorithm_env=algorithm_env,
            databases_to_use=databases_to_use,
            proxy_host=proxy_host,
        )

    def _extra_network_env(self, proxy_host: str) -> dict:
        """
        Additional environment variables related to the network setup of the
        runtime, e.g. proxy settings.

        Parameters
        ----------
        proxy_host: str
            Host of the node proxy server

        Returns
        -------
        dict
            Environment variables to add
        """
        return {}

    def _setup_environment_vars(
        self,
        algorithm_env: dict,
        databases_to_use: list[dict],
        proxy_host: str | None = None,
    ) -> dict:
        """ "
        Set environment variables required to run the algorithm

        Parameters
        ----------
        algorithm_env: dict
            Dictionary with additional environment variables to set
        databases_to_use: list[dict]
            Database selection objects to use
        proxy_host: str | None
            Proxy host to expose to the algorithm container. If None, this is
            determined from the environment.

        Returns
        -------
        dict:
            Environment variables required to run algorithm
        """
        if proxy_host is None:
            proxy_host = self._get_proxy_host()

        # define environment variables for the docker-container, the
        # host, port and api_path are from the local proxy server to
        # facilitate indirect communication with the central server
        # FIXME: we should only prepend data_folder if database_uri is a
        #   filename
        environment_variables = {
            "INPUT_FILE": f"{self.data_folder}/{self.task_folder_name}/input",
            "OUTPUT_FILE": f"{self.data_folder}/{self.task_folder_name}/output",
            "TOKEN_FILE": f"{self.data_folder}/{self.task_folder_name}/token",
            "TEMPORARY_FOLDER": self.tmp_folder,
            "HOST": f"http://{proxy_host}",
            "PORT": os.environ.get("PROXY_SERVER_PORT", 8080),
            "API_PATH": "",
        }
        safe_environment_variables = {}
        if self.write_run_context_file:
            safe_environment_variables[
                "RUN_CONTEXT_FILE"
            ] = f"{self.data_folder}/{self.task_folder_name}/run_context.json"
            # Keep this path plain for algorithms that read RUN_CONTEXT_FILE
            # directly. It is node-generated, not task input.

        # Add runtime specific network settings (e.g. proxy settings)
        environment_variables.update(self._extra_network_env(proxy_host))

        for database in databases_to_use:
            if database["label"] not in self.databases:
                # In this case the algorithm might crash if it tries to access
                # the DATABASE_LABEL environment variable
                self.log.warning(
                    "A user specified a database '%s' that does not exist. Available "
                    "databases are: %s. This is likely to result in an algorithm "
                    "crash.",
                    database,
                    self.databases.keys(),
                )
            # define env vars for the preprocessing and extra parameters such
            # as query and sheet_name
            extra_params = (
                json.loads(database.get("parameters"))
                if database.get("parameters")
                else {}
            )
            for optional_key in ["query", "sheet_name", "preprocessing"]:
                if optional_key in extra_params:
                    env_var_value = (
                        extra_params[optional_key]
                        if optional_key != "preprocessing"
                        else json.dumps(extra_params[optional_key])
                    )
                    environment_variables[
                        f"{database['label'].upper()}_{optional_key.upper()}"
                    ] = env_var_value

        environment_variables["USER_REQUESTED_DATABASE_LABELS"] = ",".join(
            [db["label"] for db in databases_to_use]
        )

        # Only prepend the data_folder is it is a file-based database
        # This allows algorithms to access multiple data sources at the
        # same time
        db_labels = []
        for label in self.databases:
            db = self.databases[label]

            uri_var_name = f"{label.upper()}_DATABASE_URI"
            environment_variables[uri_var_name] = self._database_uri_in_container(
                label, db
            )

            type_var_name = f"{label.upper()}_DATABASE_TYPE"
            environment_variables[type_var_name] = db["type"]

            # Add optional database parameter settings, these can be used by
            # the algorithm (wrapper). Note that all env keys are prefixed
            # with DB_PARAM_ to avoid collisions with other environment
            # variables.
            if "env" in db:
                for key in db["env"]:
                    env_key = f"{label.upper()}_DB_PARAM_{key.upper()}"
                    environment_variables[env_key] = db["env"][key]

            db_labels.append(label)
        environment_variables["DB_LABELS"] = ",".join(db_labels)

        # Load additional environment variables
        if algorithm_env:
            environment_variables = {**environment_variables, **algorithm_env}
            self.log.info("Custom environment variables are loaded!")
            self.log.debug(f"custom environment: {algorithm_env}")

        all_environment_variables = {
            **environment_variables,
            **safe_environment_variables,
        }
        # validate whether environment variables don't contain any illegal
        # characters
        self._validate_environment_variables(all_environment_variables)

        # print the environment before encoding it so that the user can see
        # what is passed to the container
        self.log.debug(f"environment: {all_environment_variables}")

        # encode environment variables to prevent special characters from being
        # possibly code injection
        environment_variables = self._encode_environment_variables(
            environment_variables
        )

        return {**environment_variables, **safe_environment_variables}

    def _get_proxy_host(self) -> str:
        """
        Determine host for local proxy service used by algorithm containers.

        Returns
        -------
        str
            Proxy host name for algorithm run.
        """
        try:
            return os.environ["PROXY_SERVER_HOST"]
        except Exception:
            self.log.warning("PROXY_SERVER_HOST not set, using host.docker.internal")
            self.log.debug("environment: %s", os.environ)
            return "host.docker.internal"

    # NOTE: we use `run_input` here, though the same value is called
    # `docker_input` elsewhere for now
    def _deserialize_run_input(self, run_input: bytes | str | None) -> dict:
        """
        Deserialize run input into a dictionary.

        Parameters
        ----------
        run_input: bytes | str | None
            Run input payload passed to algorithm container.

        Returns
        -------
        dict
            Deserialized input dictionary, or empty dict on failure.
        """
        if not run_input:
            return {}
        try:
            parsed = json.loads(run_input)
        except Exception:
            self.log.warning(
                "Unable to deserialize task input for run context file; using defaults"
            )
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _database_uri_in_container(self, label: str, db: dict) -> str:
        """
        Compute the URI that should be used inside the algorithm container.

        Parameters
        ----------
        label: str
            Database label.
        db: dict
            Database metadata as stored in this task manager.

        Returns
        -------
        str
            Database URI visible from inside the algorithm container.
        """
        if db.get("mount_mode") == "ro":
            return db["mount_target"]
        if db["is_file"]:
            # In dockerized mode, DockerManager._set_database() copied file
            # databases to __task_dir (the node data volume).
            # That volume is mounted at self.data_folder in the algorithm
            # container, so this URI points to the copied file.
            return f"{self.data_folder}/{os.path.basename(db['uri'])}"
        if db["is_dir"]:
            return f"/mnt/{label}"
        return db["uri"]

    def _write_run_context_file(
        self, input_data: dict, databases_to_use: list[dict], proxy_host: str
    ) -> None:
        """
        Write minimal run context file for algorithm run.

        Parameters
        ----------
        input_data: dict
            Deserialized task input.
        databases_to_use: list[dict]
            Databases selected for this task.
        proxy_host: str
            Proxy host used by algorithm containers.
        """
        run_context_path = os.path.join(self.task_folder_path, "run_context.json")

        inputs = []
        for database in databases_to_use:
            label = database.get("label")
            if not label:
                continue
            db = self.databases.get(label)
            if not db:
                self.log.warning(
                    "Database '%s' was requested but is unavailable for run context file",
                    label,
                )
                continue
            if not db.get("is_file"):
                msg = (
                    "run_context_file currently supports only file-based databases. "
                    f"Requested database '{label}' is configured as a non-file source. "
                    "Disable run_context_file for this node or switch this database "
                    "to a file-based source."
                )
                self.status = TaskStatus.START_FAILED
                self.log.error(msg)
                raise PermanentAlgorithmStartFail(msg)

            input_item = {
                "id": label,
                "uri": self._database_uri_in_container(label, db),
            }
            if db.get("type"):
                input_item["type"] = db["type"]

            if database.get("parameters"):
                try:
                    # The server stores all non-label database request fields
                    # in TaskDatabase.parameters, including `query`,
                    # `sheet_name` and `arguments`.
                    database_parameters = json.loads(database["parameters"])
                except Exception:
                    self.log.warning(
                        "Unable to parse parameters for database '%s' in run context",
                        label,
                    )
                else:
                    # For run context, only surface the nested per-input `arguments`
                    # object, not the other database request fields.
                    # As we mature this run context idea, this might change.
                    arguments = (
                        database_parameters.get("arguments")
                        if isinstance(database_parameters, dict)
                        else None
                    )
                    if isinstance(arguments, dict):
                        input_item["arguments"] = arguments

            inputs.append(input_item)

        output_uri = f"{self.data_folder}/{self.task_folder_name}/output"
        token_file = f"{self.data_folder}/{self.task_folder_name}/token"
        run_context = {
            "schema_version": "0.1",
            "entrypoint": {
                "name": input_data.get("method", ""),
            },
            "arguments": {
                "positional": input_data.get("args", []),
                "named": input_data.get("kwargs", {}),
            },
            "executor": {
                "id": self.node_id,
                "kind": "vantage6-node",
            },
            "inputs": inputs,
            "outputs": [
                {
                    "id": "result",
                    "uri": output_uri,
                }
            ],
            "x-vantage6": {
                "run_id": self.run_id,
                "task_id": self.task_id,
                "collaboration_id": self.collaboration_id,
                "token_file": token_file,
                "api_proxy": {
                    "host": f"http://{proxy_host}",
                    "port": str(os.environ.get("PROXY_SERVER_PORT", 8080)),
                    "api_path": "",
                },
                "temporary_directory": self.tmp_folder,
            },
        }

        with open(run_context_path, "w", encoding="utf-8") as fp:
            json.dump(run_context, fp, indent=2)
            fp.write("\n")
        if self.task_file_mode is not None:
            os.chmod(run_context_path, self.task_file_mode)
        self.log.debug("Wrote run context file to '%s'", run_context_path)

    def _validate_environment_variables(self, environment_variables: dict) -> None:
        """
        Check whether environment variables don't contain any illegal
        characters

        Parameters
        ----------
        environment_variables: dict
            Environment variables required to run algorithm

        Raises
        ------
        PermanentAlgorithmStartFail
            If environment variables contain illegal characters
        """
        msg = None
        for key in environment_variables:
            if not key.isidentifier():
                msg = (
                    f"Environment variable '{key}' is invalid: environment "
                    " variable names should only contain number, letters and "
                    " underscores, and start with a letter."
                )
            elif key in ENV_VARS_NOT_SETTABLE_BY_NODE:
                msg = (
                    f"Environment variable '{key}' cannot be set: this "
                    "variable is set in the algorithm Dockerfile and cannot "
                    "be overwritten."
                )
            if msg:
                self.status = TaskStatus.FAILED
                self.log.error(msg)
                raise PermanentAlgorithmStartFail(msg)

    def _encode_environment_variables(self, environment_variables: dict) -> dict:
        """
        Encode environment variable values to ensure that special characters
        are not interpretable as code while transferring them to the algorithm
        container.

        Parameters
        ----------
        environment_variables: dict
            Environment variables required to run algorithm

        Returns
        -------
        dict:
            Environment variables with encoded values
        """

        def _encode(string: str) -> str:
            """Encode env var value

            We first encode to bytes, then to b32 and then decode to a string.
            Finally, '=' is replaced by less sensitve characters to prevent
            issues with interpreting the encoded string in the env var value.

            Parameters
            ----------
            string: str
                String to be encoded

            Returns
            -------
            str:
                Encoded string

            Examples
            --------
            >>> _encode("abc")
            'MFRGG!!!'
            """
            return (
                base64.b32encode(string.encode(STRING_ENCODING))
                .decode(STRING_ENCODING)
                .replace("=", ENV_VAR_EQUALS_REPLACEMENT)
            )

        self.log.debug("Encoding environment variables")

        encoded_environment_variables = {}
        for key, val in environment_variables.items():
            encoded_environment_variables[key] = _encode(str(val))
        return encoded_environment_variables
