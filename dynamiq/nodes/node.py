import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime
from functools import cached_property
from queue import Empty
from typing import Any, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from dynamiq.cache.utils import cache_wf_entity
from dynamiq.callbacks import BaseCallbackHandler
from dynamiq.connections import BaseConnection
from dynamiq.connections.managers import ConnectionManager
from dynamiq.nodes.exceptions import (
    NodeConditionFailedException,
    NodeException,
    NodeFailedException,
    NodeSkippedException,
)
from dynamiq.nodes.types import NodeGroup
from dynamiq.runnables import Runnable, RunnableConfig, RunnableResult, RunnableStatus
from dynamiq.storages.vector.base import BaseVectorStoreParams
from dynamiq.types.streaming import STREAMING_EVENT, StreamingConfig, StreamingEventMessage
from dynamiq.utils import format_value, generate_uuid, merge
from dynamiq.utils.duration import format_duration
from dynamiq.utils.jsonpath import filter as jsonpath_filter
from dynamiq.utils.jsonpath import mapper as jsonpath_mapper
from dynamiq.utils.logger import logger


def ensure_config(config: RunnableConfig = None) -> RunnableConfig:
    """
    Ensure that a valid RunnableConfig is provided.

    Args:
        config (RunnableConfig, optional): The input configuration. Defaults to None.

    Returns:
        RunnableConfig: A valid RunnableConfig object.
    """
    if config is None:
        return RunnableConfig(callbacks=[])

    return config


class ErrorHandling(BaseModel):
    """
    Configuration for error handling in nodes.

    Attributes:
        timeout_seconds (float | None): Timeout in seconds for node execution.
        retry_interval_seconds (float): Interval between retries in seconds.
        max_retries (int): Maximum number of retries.
        backoff_rate (float): Rate of increase for retry intervals.
    """
    timeout_seconds: float | None = None
    retry_interval_seconds: float = 1
    max_retries: int = 0
    backoff_rate: float = 1


class Transformer(BaseModel):
    """
    Base class for input and output transformers.

    Attributes:
        path (str | None): JSONPath for data selection.
        selector (dict[str, str] | None): Mapping for data transformation.
    """
    path: str | None = None
    selector: dict[str, str] | None = None


class InputTransformer(Transformer):
    """Input transformer for nodes."""
    pass


class OutputTransformer(InputTransformer):
    """Output transformer for nodes."""
    pass


class CachingConfig(BaseModel):
    """
    Configuration for node caching.

    Attributes:
        enabled (bool): Whether caching is enabled for the node.
    """
    enabled: bool = False


class NodeReadyToRun(BaseModel):
    """
    Represents a node ready to run with its input data and dependencies.

    Attributes:
        node (Node): The node to be run.
        is_ready (bool): Whether the node is ready to run.
        input_data (Any): Input data for the node.
        depends_result (dict[str, Any]): Results of dependent nodes.
    """
    node: "Node"
    is_ready: bool
    input_data: Any = None
    depends_result: dict[str, Any] = {}

    model_config = ConfigDict(arbitrary_types_allowed=True)


class NodeDependency(BaseModel):
    """
    Represents a dependency between nodes.

    Attributes:
        node (Node): The dependent node.
        option (str | None): Optional condition for the dependency.
    """
    node: "Node"
    option: str | None = None

    def __init__(self, node: "Node", option: str | None = None):
        super().__init__(node=node, option=option)

    def to_dict(self, **kwargs) -> dict:
        """Converts the instance to a dictionary.

        Returns:
            dict: A dictionary representation of the instance.
        """
        return {
            "node": self.node.to_dict(**kwargs),
            "option": self.option,
        }


class NodeMetadata(BaseModel):
    """
    Metadata for a node.

    Attributes:
        label (str | None): Optional label for the node.
    """
    label: str | None = None


class Node(BaseModel, Runnable, ABC):
    """
    Abstract base class for all nodes in the workflow.

    Attributes:
        id (str): Unique identifier for the node.
        name (str | None): Optional name for the node.
        group (NodeGroup): Group the node belongs to.
        description (str | None): Optional description for the node.
        error_handling (ErrorHandling): Error handling configuration.
        input_transformer (InputTransformer): Input data transformer.
        output_transformer (OutputTransformer): Output data transformer.
        caching (CachingConfig): Caching configuration.
        depends (list[NodeDependency]): List of node dependencies.
        metadata (NodeMetadata | None): Optional metadata for the node.
        is_postponed_component_init (bool): Whether component initialization is postponed.
        is_optimized_for_agents (bool): Whether to optimize output for agents. By default is set to False.
    """
    id: str = Field(default_factory=generate_uuid)
    name: str | None = None
    description: str | None = None
    group: NodeGroup
    error_handling: ErrorHandling = ErrorHandling()
    input_transformer: InputTransformer = InputTransformer()
    output_transformer: OutputTransformer = OutputTransformer()
    caching: CachingConfig = CachingConfig()
    streaming: StreamingConfig = StreamingConfig()
    depends: list[NodeDependency] = []
    metadata: NodeMetadata | None = None

    is_postponed_component_init: bool = False
    is_optimized_for_agents: bool = False

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.is_postponed_component_init:
            self.init_components()

    @computed_field
    @cached_property
    def type(self) -> str:
        return f"{self.__module__.rsplit('.', 1)[0]}.{self.__class__.__name__}"

    def _validate_dependency_status(
        self, depend: NodeDependency, depends_result: dict[str, RunnableResult]
    ):
        """
        Validate the status of a dependency.

        Args:
            depend (NodeDependency): The dependency to validate.
            depends_result (dict[str, RunnableResult]): Results of dependent nodes.

        Raises:
            NodeException: If the dependency result is missing.
            NodeFailedException: If the dependency failed.
            NodeSkippedException: If the dependency was skipped.
        """
        if not (dep_result := depends_result.get(depend.node.id)):
            raise NodeException(
                failed_depend=depend,
                message=f"Dependency {depend.node.id}: result missed",
            )

        if dep_result.status == RunnableStatus.FAILURE:
            raise NodeFailedException(
                failed_depend=depend, message=f"Dependency {depend.node.id}: failed"
            )

        if dep_result.status == RunnableStatus.SKIP:
            raise NodeSkippedException(
                failed_depend=depend, message=f"Dependency {depend.node.id}: skipped"
            )

    def _validate_dependency_condition(
        self, depend: NodeDependency, depends_result: dict[str, RunnableResult]
    ):
        """
        Validate the condition of a dependency.

        Args:
            depend (NodeDependency): The dependency to validate.
            depends_result (dict[str, RunnableResult]): Results of dependent nodes.

        Raises:
            NodeConditionFailedException: If the dependency condition is not met.
        """
        if (dep_output_data := depends_result.get(depend.node.id)) and isinstance(
            dep_output_data.output, dict
        ):
            if (
                dep_condition_data := dep_output_data.output.get(depend.option)
            ) and dep_condition_data.status == RunnableStatus.FAILURE:
                raise NodeConditionFailedException(
                    failed_depend=depend,
                    message=f"Dependency {depend.node.id} condition {depend.option}: result is false",
                )

    def validate_depends(self, depends_result):
        """
        Validate all dependencies of the node.

        Args:
            depends_result (dict): Results of dependent nodes.

        Raises:
            Various exceptions based on dependency validation results.
        """
        for dep in self.depends:
            self._validate_dependency_status(depend=dep, depends_result=depends_result)
            if dep.option:
                self._validate_dependency_condition(
                    depend=dep, depends_result=depends_result
                )

    def init_components(
        self, connection_manager: ConnectionManager = ConnectionManager()
    ):
        """
        Initialize node components.

        Args:
            connection_manager (ConnectionManager, optional): Connection manager instance.
                Defaults to ConnectionManager().
        """
        self.is_postponed_component_init = False

    @staticmethod
    def transform(data: Any, transformer: Transformer, node_id: str) -> Any:
        """
        Apply transformation to data.

        Args:
            data (Any): Input data to transform.
            transformer (Transformer): Transformer to apply.
            node_id (str): ID of the node performing the transformation.

        Returns:
            Any: Transformed data.
        """
        output = jsonpath_filter(data, transformer.path, node_id)
        output = jsonpath_mapper(output, transformer.selector, node_id)
        return output

    def transform_input(self, input_data: Any) -> Any:
        """
        Transform input data for the node.

        Args:
            input_data (Any): Input data to transform.

        Returns:
            Any: Transformed input data.
        """
        return self.transform(input_data, self.input_transformer, self.id)

    def transform_output(self, output_data: Any) -> Any:
        """
        Transform output data from the node.

        Args:
            output_data (Any): Output data to transform.

        Returns:
            Any: Transformed output data.
        """
        return self.transform(output_data, self.output_transformer, self.id)

    @property
    def to_dict_exclude_params(self):
        return {
            "client": True,
            "vector_store": True,
            "connection": {"api_key": True},
            "depends": True,
        }

    def to_dict(self, **kwargs) -> dict:
        """Converts the instance to a dictionary.

        Returns:
            dict: A dictionary representation of the instance.
        """
        data = self.model_dump(
            exclude=kwargs.pop("exclude", self.to_dict_exclude_params),
            serialize_as_any=kwargs.pop("serialize_as_any", True),
            **kwargs,
        )
        data["depends"] = [depend.to_dict(**kwargs) for depend in self.depends]
        return data

    def run(
        self,
        input_data: Any,
        config: RunnableConfig = None,
        depends_result: dict = None,
        **kwargs,
    ) -> RunnableResult:
        """
        Run the node with given input data and configuration.

        Args:
            input_data (Any): Input data for the node.
            config (RunnableConfig, optional): Configuration for the run. Defaults to None.
            depends_result (dict, optional): Results of dependent nodes. Defaults to None.
            **kwargs: Additional keyword arguments.

        Returns:
            RunnableResult: Result of the node execution.
        """
        from dynamiq.nodes.agents.exceptions import RecoverableAgentException

        logger.info(f"Node {self.name} - {self.id}: execution started.")
        time_start = datetime.now()

        config = ensure_config(config)
        merged_kwargs = merge(
            kwargs, {"run_id": uuid4(), "parent_run_id": kwargs.get("parent_run_id")}
        )
        if depends_result is None:
            depends_result = {}

        transformed_input = input_data | {k: result.to_tracing_depend_dict() for k, result in depends_result.items()}

        try:
            self.validate_depends(depends_result)
        except NodeException as e:
            skip_data = {"failed_dependency": e.failed_depend.to_dict()}
            self.run_on_node_skip(
                callbacks=config.callbacks,
                skip_data=skip_data,
                input_data=transformed_input,
                **merged_kwargs,
            )
            logger.info(f"Node {self.name} - {self.id}: execution skipped.")
            return RunnableResult(
                status=RunnableStatus.SKIP,
                input=transformed_input,
                output=format_value(e),
            )

        try:
            if self.input_transformer.path or self.input_transformer.selector:
                depends_result_as_dict = {k: result.to_depend_dict() for k, result in depends_result.items()}

                transformed_input = self.transform_input(input_data | depends_result_as_dict)

            self.run_on_node_start(config.callbacks, transformed_input, **merged_kwargs)
            cache = cache_wf_entity(
                entity_id=self.id,
                cache_enabled=self.caching.enabled,
                cache_config=config.cache,
            )
            output, from_cache = cache(self.execute_with_retry)(
                transformed_input, config, **merged_kwargs
            )

            merged_kwargs["is_output_from_cache"] = from_cache
            transformed_output = self.transform_output(output)
            self.run_on_node_end(config.callbacks, transformed_output, **merged_kwargs)

            logger.info(
                f"Node {self.name} - {self.id}: execution succeeded in "
                f"{format_duration(time_start, datetime.now())}."
            )
            return RunnableResult(
                status=RunnableStatus.SUCCESS,
                input=transformed_input,
                output=transformed_output,
            )
        except Exception as e:
            self.run_on_node_error(config.callbacks, e, **merged_kwargs)
            logger.error(
                f"Node {self.name} - {self.id}: execution failed in "
                f"{format_duration(time_start, datetime.now())}."
            )

            recoverable = isinstance(e, RecoverableAgentException)
            return RunnableResult(
                status=RunnableStatus.FAILURE,
                input=input_data,
                output=format_value(e, recoverable=recoverable),
            )

    def execute_with_retry(
        self, input_data: dict[str, Any], config: RunnableConfig = None, **kwargs
    ):
        """
        Execute the node with retry logic.

        Args:
            input_data (dict[str, Any]): Input data for the node.
            config (RunnableConfig, optional): Configuration for the execution. Defaults to None.
            **kwargs: Additional keyword arguments.

        Returns:
            Any: Result of the node execution.

        Raises:
            Exception: If all retry attempts fail.
        """
        config = ensure_config(config)

        error = None
        n_attempt = self.error_handling.max_retries + 1
        for attempt in range(n_attempt):
            merged_kwargs = merge(kwargs, {"execution_run_id": uuid4()})
            self.run_on_node_execute_start(
                config.callbacks, input_data, **merged_kwargs
            )

            try:
                output = self.execute_with_timeout(
                    self.error_handling.timeout_seconds,
                    input_data,
                    config,
                    **merged_kwargs,
                )

                self.run_on_node_execute_end(config.callbacks, output, **merged_kwargs)
                return output
            except TimeoutError as e:
                error = e
                self.run_on_node_execute_error(config.callbacks, error, **merged_kwargs)
                logger.warning(f"Node {self.name} - {self.id}: timeout.")
            except Exception as e:
                error = e
                self.run_on_node_execute_error(config.callbacks, error, **merged_kwargs)
                logger.error(f"Node {self.name} - {self.id}: execution error: {e}")

            # do not sleep after the last attempt
            if attempt < n_attempt - 1:
                time_to_sleep = self.error_handling.retry_interval_seconds * (
                    self.error_handling.backoff_rate**attempt
                )
                logger.info(
                    f"Node {self.name} - {self.id}: retrying in {time_to_sleep} seconds."
                )
                time.sleep(time_to_sleep)

        logger.error(
            f"Node {self.name} - {self.id}: execution failed after {n_attempt} attempts."
        )
        raise error

    def execute_with_timeout(
        self,
        timeout: float | None,
        input_data: dict[str, Any],
        config: RunnableConfig = None,
        **kwargs,
    ):
        """
        Execute the node with a timeout.

        Args:
            timeout (float | None): Timeout duration in seconds.
            input_data (dict[str, Any]): Input data for the node.
            config (RunnableConfig, optional): Configuration for the runnable.
            **kwargs: Additional keyword arguments.

        Returns:
            Any: Result of the execution.

        Raises:
            Exception: If execution fails or times out.
        """
        with ThreadPoolExecutor() as executor:
            future = executor.submit(self.execute, input_data, config=config, **kwargs)

            try:
                result = future.result(timeout=timeout)
            except Exception as e:
                raise e

            return result

    def get_input_streaming_event(
        self,
        event_msg_type: "type[StreamingEventMessage]" = StreamingEventMessage,
        event: str | None = None,
        config: RunnableConfig = None,
    ) -> StreamingEventMessage:
        """
        Get the input streaming event from the input streaming.

        Args:
            event_msg_type (Type[StreamingEventMessage], optional): The event message type to use.
            event (str, optional): The event to use for the message.
            config (RunnableConfig, optional): Configuration for the runnable.

        """
        # Use runnable streaming configuration. If not found use node streaming configuration
        streaming = getattr(config.nodes_override.get(self.id), "streaming", None) or self.streaming
        if streaming.input_streaming_enabled:
            while not streaming.input_queue_done_event or not streaming.input_queue_done_event.is_set():
                try:
                    data = streaming.input_queue.get(timeout=streaming.timeout)
                except Empty:
                    raise ValueError(f"Input streaming timeout: {streaming.timeout} exceeded.")

                try:
                    event_msg = event_msg_type.model_validate_json(data)
                    if event and event_msg.event != event:
                        raise ValueError()
                except ValueError:
                    logger.error(
                        f"Invalid streaming event data: {data}. "
                        f"Allowed event: {event}, event_msg_type: {event_msg_type}"
                    )
                    continue

                return event_msg

        raise ValueError("Input streaming is not enabled.")

    def run_on_node_start(
        self,
        callbacks: list[BaseCallbackHandler],
        input_data: dict[str, Any],
        **kwargs,
    ):
        """
        Run callbacks on node start.

        Args:
            callbacks (list[BaseCallbackHandler]): List of callback handlers.
            input_data (dict[str, Any]): Input data for the node.
            **kwargs: Additional keyword arguments.
        """
        for callback in callbacks:
            callback.on_node_start(self.to_dict(), input_data, **kwargs)

    def run_on_node_end(
        self,
        callbacks: list[BaseCallbackHandler],
        output_data: dict[str, Any],
        **kwargs,
    ):
        """
        Run callbacks on node end.

        Args:
            callbacks (list[BaseCallbackHandler]): List of callback handlers.
            output_data (dict[str, Any]): Output data from the node.
            **kwargs: Additional keyword arguments.
        """
        for callback in callbacks:
            callback.on_node_end(self.model_dump(), output_data, **kwargs)

    def run_on_node_error(
        self,
        callbacks: list[BaseCallbackHandler],
        error: BaseException,
        **kwargs,
    ):
        """
        Run callbacks on node error.

        Args:
            callbacks (list[BaseCallbackHandler]): List of callback handlers.
            error (BaseException): The error that occurred.
            **kwargs: Additional keyword arguments.
        """
        for callback in callbacks:
            callback.on_node_error(self.to_dict(), error, **kwargs)

    def run_on_node_skip(
        self,
        callbacks: list[BaseCallbackHandler],
        skip_data: dict[str, Any],
        input_data: dict[str, Any],
        **kwargs,
    ):
        """
        Run callbacks on node skip.

        Args:
            callbacks (list[BaseCallbackHandler]): List of callback handlers.
            skip_data (dict[str, Any]): Data related to the skip.
            input_data (dict[str, Any]): Input data for the node.
            **kwargs: Additional keyword arguments.
        """
        for callback in callbacks:
            callback.on_node_skip(self.to_dict(), skip_data, input_data, **kwargs)

    def run_on_node_execute_start(
        self,
        callbacks: list[BaseCallbackHandler],
        input_data: dict[str, Any],
        **kwargs,
    ):
        """
        Run callbacks on node execute start.

        Args:
            callbacks (list[BaseCallbackHandler]): List of callback handlers.
            input_data (dict[str, Any]): Input data for the node.
            **kwargs: Additional keyword arguments.
        """
        for callback in callbacks:
            callback.on_node_execute_start(self.to_dict(), input_data, **kwargs)

    def run_on_node_execute_end(
        self,
        callbacks: list[BaseCallbackHandler],
        output_data: dict[str, Any],
        **kwargs,
    ):
        """
        Run callbacks on node execute end.

        Args:
            callbacks (list[BaseCallbackHandler]): List of callback handlers.
            output_data (dict[str, Any]): Output data from the node.
            **kwargs: Additional keyword arguments.
        """
        for callback in callbacks:
            callback.on_node_execute_end(self.to_dict(), output_data, **kwargs)

    def run_on_node_execute_error(
        self,
        callbacks: list[BaseCallbackHandler],
        error: BaseException,
        **kwargs,
    ):
        """
        Run callbacks on node execute error.

        Args:
            callbacks (list[BaseCallbackHandler]): List of callback handlers.
            error (BaseException): The error that occurred.
            **kwargs: Additional keyword arguments.
        """
        for callback in callbacks:
            callback.on_node_execute_error(self.model_dump(), error, **kwargs)

    def run_on_node_execute_run(
        self,
        callbacks: list[BaseCallbackHandler],
        **kwargs,
    ):
        """
        Run callbacks on node execute run.

        Args:
            callbacks (list[BaseCallbackHandler]): List of callback handlers.
            **kwargs: Additional keyword arguments.
        """
        for callback in callbacks:
            callback.on_node_execute_run(self.to_dict(), **kwargs)

    def run_on_node_execute_stream(
        self,
        callbacks: list[BaseCallbackHandler],
        chunk: dict[str, Any] | None = None,
        **kwargs,
    ):
        """
        Run callbacks on node execute stream.

        Args:
            callbacks (list[BaseCallbackHandler]): List of callback handlers.
            chunk (dict[str, Any]): Chunk of streaming data.
            **kwargs: Additional keyword arguments.
        """
        for callback in callbacks:
            callback.on_node_execute_stream(self.to_dict(), chunk, **kwargs)

    @abstractmethod
    def execute(
        self, input_data: dict[str, Any], config: RunnableConfig = None, **kwargs
    ) -> Any:
        """
        Execute the node with the given input.

        Args:
            input_data (dict[str, Any]): Input data for the node.
            config (RunnableConfig, optional): Configuration for the runnable.
            **kwargs: Additional keyword arguments.

        Returns:
            Any: Result of the execution.
        """
        pass

    def depends_on(self, nodes: Union["Node", list["Node"]]):
        """
        Add dependencies for this node. Accepts either a single node or a list of nodes.

        Args:
            nodes (Node or list[Node]): A single node or list of nodes this node depends on.

        Raises:
            TypeError: If the input is neither a Node nor a list of Node instances.
            ValueError: If an empty list is provided.

        Returns:
            self: Enables method chaining.
        """

        if nodes is None:
            raise ValueError("Nodes cannot be None.")

        # If a single node is provided, convert it to a list
        if isinstance(nodes, Node):
            nodes = [nodes]

        # Ensure the input is a list of Node instances
        if not isinstance(nodes, list) or not all(isinstance(node, Node) for node in nodes):
            raise TypeError(f"Expected a Node or a list of Node instances, but got {type(nodes).__name__}.")

        if not nodes:
            raise ValueError("Cannot add an empty list of dependencies.")

        # Add each node as a dependency
        for node in nodes:
            self.depends.append(NodeDependency(node))

        return self  # enable chaining

    def enable_streaming(self, event: str = STREAMING_EVENT):
        """
        Enable streaming for the node and optionally set the event name.

        Args:
            event (str): The event name for streaming. Defaults to 'streaming'.

        Returns:
            self: Enables method chaining.
        """
        self.streaming.enabled = True
        self.streaming.event = event
        return self


class ConnectionNode(Node, ABC):
    """
    Abstract base class for nodes that require a connection.

    Attributes:
        connection (BaseConnection | None): The connection to use.
        client (Any | None): The client instance.
    """

    connection: BaseConnection | None = None
    client: Any | None = None

    @model_validator(mode="after")
    def validate_connection_client(self):
        """Validate that either connection or client is specified."""
        if not self.client and not self.connection:
            raise ValueError("'connection' or 'client' should be specified")
        return self

    def init_components(
        self, connection_manager: ConnectionManager = ConnectionManager()
    ):
        """
        Initialize components for the node.

        Args:
            connection_manager (ConnectionManager): The connection manager to use.
        """
        super().init_components(connection_manager)
        if self.client is None:
            self.client = connection_manager.get_connection_client(
                connection=self.connection
            )


class VectorStoreNode(ConnectionNode, BaseVectorStoreParams, ABC):
    vector_store: Any | None = None

    @model_validator(mode="after")
    def validate_connection_client(self):
        if not self.vector_store and not self.connection:
            raise ValueError("'connection' or 'vector_store' should be specified")

    @property
    @abstractmethod
    def vector_store_cls(self):
        raise NotImplementedError

    @property
    def vector_store_params(self):
        return self.model_dump(include=set(BaseVectorStoreParams.model_fields)) | {
            "client": self.client
        }

    def connect_to_vector_store(self):
        vector_store_params = self.vector_store_params
        vector_store = self.vector_store_cls(**vector_store_params)

        logger.debug(
            f"Node {self.name} - {self.id}: connected to {self.vector_store_cls.__name__} vector store with"
            f" {vector_store_params}"
        )

        return vector_store

    def init_components(
        self, connection_manager: ConnectionManager = ConnectionManager()
    ):
        """
        Initialize components for the node.

        Args:
            connection_manager (ConnectionManager): The connection manager to use.
        """
        # Use vector_store client if it is already initialized
        if self.vector_store:
            self.client = self.vector_store.client

        super().init_components(connection_manager)

        if self.vector_store is None:
            self.vector_store = self.connect_to_vector_store()