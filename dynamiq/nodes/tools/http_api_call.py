import enum
from typing import Any, Literal
from urllib.parse import urljoin

from pydantic import Field

from dynamiq.connections import Http as HttpConnection
from dynamiq.nodes import NodeGroup
from dynamiq.nodes.node import ConnectionNode, ensure_config
from dynamiq.runnables import RunnableConfig


class ResponseType(str, enum.Enum):
    TEXT = "text"
    RAW = "raw"
    JSON = "json"


class HttpApiCall(ConnectionNode):
    """
    A component for sending API requests using requests library.

    Attributes:
        group (Literal[NodeGroup.TOOLS]): The group the node belongs to.
        connection (HttpConnection | None): The connection based on sending http requests.A new connection
            is created if none is provided.
        success_codes(list[int]): The list of codes when request is successful.
        timeout (float): The timeout in seconds.
        data(dict[str,Any]): The data to send as body of request.
        headers(dict[str,Any]): The headers of request.
        params(dict[str,Any]): The additional query params of request.
        response_type(ResponseType|str): The type of response content.
    """

    name: str = "The name of the API call tool"
    description: str = "The description of the API call tool"
    group: Literal[NodeGroup.TOOLS] = NodeGroup.TOOLS
    connection: HttpConnection
    success_codes: list[int] = [200]
    timeout: float = 30
    data: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    response_type: ResponseType | str | None = ResponseType.RAW

    def execute(
        self, input_data: dict[str, Any], config: RunnableConfig = None, **kwargs
    ):
        """Execute the API call.

        This method takes input data and returns content of API call response.

        Args:
            input_data (dict[str, Any]): The input data containing(optionally) data, headers,
                params for request.
            config (RunnableConfig, optional): Configuration for the execution. Defaults to None.
            **kwargs: Additional keyword arguments.

        Returns:
             dict: A dictionary with the following keys:
                - "content" (bytes|string|dict[str,Any]): Value containing the result of request.
                - "status_code" (int): The status code of the request.
        """
        config = ensure_config(config)
        self.run_on_node_execute_run(config.callbacks, **kwargs)
        data = input_data.get("data", {})
        url = self.connection.url
        if url_path := input_data.get("url_path", ""):
            url = urljoin(url, url_path)
        headers = input_data.get("headers", {})
        params = input_data.get("params", {})
        response = self.client.request(
            method=self.connection.method,
            url=url,
            headers=self.connection.headers | self.headers | headers,
            params=self.connection.params | self.params | params,
            data=self.connection.data | self.data | data,
            timeout=self.timeout,
        )
        if response.status_code not in self.success_codes:
            raise ValueError(
                f"Request failed with unexpected status code: {response.status_code} and response: {response.text}"
            )

        response_type = self.response_type
        if (
            "response_type" not in self.model_fields_set
            and response.headers.get("content-type") == "application/json"
        ):
            response_type = ResponseType.JSON

        if response_type == ResponseType.TEXT:
            content = response.text
        elif response_type == ResponseType.RAW:
            content = response.content
        elif response_type == ResponseType.JSON:
            content = response.json()
        else:
            allowed_types = [item.value for item in ResponseType]
            raise ValueError(
                f"Response type must be one of the following: {', '.join(allowed_types)}"
            )
        return {"content": content, "status_code": response.status_code}