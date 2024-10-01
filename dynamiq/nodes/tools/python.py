import importlib
import io
from typing import Any, Literal

from RestrictedPython import compile_restricted, safe_builtins, utility_builtins
from RestrictedPython.Eval import default_guarded_getattr, default_guarded_getitem, default_guarded_getiter
from RestrictedPython.Guards import guarded_unpack_sequence

from dynamiq.nodes import Node, NodeGroup
from dynamiq.nodes.node import ensure_config
from dynamiq.runnables import RunnableConfig
from dynamiq.utils.logger import logger

ALLOWED_MODULES = [
    "base64",
    "collections",
    "copy",
    "cmath",
    "csv",
    "datetime",
    "functools",
    "itertools",
    "json",
    "math",
    "operator",
    "pydantic",
    "random",
    "re",
    "requests",
    "statistics",
    "time",
    "typing",
    "urllib",
    "uuid",
]


def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name not in ALLOWED_MODULES:
        raise ImportError(f"Import of '{name}' is not allowed")
    return importlib.import_module(name)


class Python(Node):
    """
    Node for executing Python code in a secure sandbox.

    Attributes:
        group (Literal[NodeGroup.TOOLS]): Group for the node. Defaults to NodeGroup.TOOLS.
        name (str): Name of the node. Defaults to "Python Code Executor".
        base_description (str): Base description of the node.
        description (str): Description of the node.
        code (str): Python code to execute.
    """
    group: Literal[NodeGroup.TOOLS] = NodeGroup.TOOLS
    name: str = "Python Code Executor"
    base_description: str = (
        "The tool, that executes Python code in a secure sandbox environment."
        "All arguments are passed as a dictionary to the 'run' main function."
    )
    description: str = ""
    code: str

    def __init__(self, **data):
        """Initialize the Python node.

        Args:
            **data: Additional keyword arguments.
        """
        super().__init__(**data)
        self.description = f"{self.base_description}.{self.description}"

    def execute(self, input_data: dict[str, Any], config: RunnableConfig = None, **kwargs) -> Any:
        """Execute the Python code.

        Args:
            input_data (dict[str, Any]): Input data for the code.
            config (RunnableConfig, optional): Configuration for the execution.
            **kwargs: Additional keyword arguments.

        Returns:
            Any: Result of the code execution.
        """
        logger.debug(f"Tool {self.name} - {self.id}: started with input data {input_data}")

        config = ensure_config(config)
        self.run_on_node_execute_run(config.callbacks, **kwargs)
        stdout = io.StringIO()

        def safe_print(*args, **kwargs):
            print(*args, file=stdout, **kwargs)

        def guarded_write(obj, value):
            """Guard to allow writing a value to an object."""
            obj = value
            return obj

        restricted_globals = {
            "__builtins__": {
                **safe_builtins,
                **utility_builtins,
                "abs": abs,
                "all": all,
                "any": any,
                "bin": bin,
                "bool": bool,
                "chr": chr,
                "complex": complex,
                "dict": dict,
                "divmod": divmod,
                "enumerate": enumerate,
                "filter": filter,
                "float": float,
                "format": format,
                "frozenset": frozenset,
                "hex": hex,
                "int": int,
                "isinstance": isinstance,
                "issubclass": issubclass,
                "len": len,
                "list": list,
                "map": map,
                "max": max,
                "min": min,
                "next": next,
                "oct": oct,
                "ord": ord,
                "pow": pow,
                "range": range,
                "reversed": reversed,
                "round": round,
                "set": set,
                "slice": slice,
                "sorted": sorted,
                "str": str,
                "sum": sum,
                "super": super,
                "tuple": tuple,
                "type": type,
                "zip": zip,
                "_getattr_": default_guarded_getattr,
                "_getitem_": default_guarded_getitem,
                "_getiter_": default_guarded_getiter,
                "__import__": restricted_import,
            },
            "_getattr_": default_guarded_getattr,
            "_inplacevar_": self._inplacevar,
            "_unpack_sequence_": guarded_unpack_sequence,
            "_iter_unpack_sequence_": guarded_unpack_sequence,
            "safe_print": safe_print,
            "_write_": guarded_write,
        }

        try:
            byte_code = compile_restricted(self.code, "<inline>", "exec")
            exec(byte_code, restricted_globals)

            if "run" not in restricted_globals:
                raise ValueError("The 'run' function is not defined in the provided code.")

            result = restricted_globals["run"](**input_data)
            return {"content": str(result)}
        except Exception as e:
            logger.error(f"Code execution error: {str(e)}")
            return {"content": "Code execution error: '{str(e)}'"}

    @staticmethod
    def _inplacevar(op, x, y):
        """Perform in-place operation.

        Args:
            op (str): Operation to perform.
            x (Any): First operand.
            y (Any): Second operand.

        Returns:
            Any: Result of the operation.
        """
        operators = {
            "+=": lambda a, b: a + b,
            "-=": lambda a, b: a - b,
            "*=": lambda a, b: a * b,
            "/=": lambda a, b: a / b,
            "//=": lambda a, b: a // b,
            "%=": lambda a, b: a % b,
            "**=": lambda a, b: a**b,
            "<<=": lambda a, b: a << b,
            ">>=": lambda a, b: a >> b,
            "&=": lambda a, b: a & b,
            "^=": lambda a, b: a ^ b,
            "|=": lambda a, b: a | b,
        }
        if op in operators:
            return operators[op](x, y)
        raise ValueError(f"Unsupported in-place operation: {op}")