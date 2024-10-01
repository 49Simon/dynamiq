import json
import re
from typing import Any

from pydantic import Field

from dynamiq.nodes.agents.base import Agent, AgentIntermediateStep, AgentIntermediateStepModelObservation
from dynamiq.nodes.agents.exceptions import ActionParsingException, AgentMaxLoopsReached, RecoverableAgentException
from dynamiq.nodes.node import NodeDependency
from dynamiq.nodes.types import InferenceMode
from dynamiq.prompts import Message, Prompt
from dynamiq.runnables import RunnableConfig, RunnableStatus
from dynamiq.utils.logger import logger

REACT_BLOCK_TOOLS = """
You have access to a variety of tools, and you are responsible for using them in any order you choose to complete the task:
{tools_desc}
"""  # noqa: E501

REACT_BLOCK_NO_TOOLS = """
You do not have access to any tools.
"""  # noqa: E501

REACT_BLOCK_XML_INSTRUCTIONS = """
Here is how you will think about the user's request
<output>
    <thought>
        Here you reason about the next step
    </thought>
    <action>
        Here you choose the tool to use from [{tools_name}]
    </action>
    <action_input>
        Here you provide the input to the tool, correct JSON format
    </action_input>
</output>

REMEMBER:
* Inside 'action' provide just name of one tool from this list: [{tools_name}]

After each action, the user will provide an "Observation" with the result.
Continue this Thought/Action/Action Input/Observation sequence until you have enough information to answer the request.

When you have sufficient information, provide your final answer in one of these two formats:
If you can answer the request:
<output>
    <thought>
        I can answer without using any tools
    </thought>
    <answer>
        Your answer here
    </answer>
</output>

If you cannot answer the request:


<output>
    <thought>
        I cannot answer with the tools I have
    </thought>
    <answer>
        Explanation of why you cannot answer
    </answer>
</output>


"""  # noqa: E501


REACT_BLOCK_INSTRUCTIONS = """
Always structure your responses in the following format:

Thought: [Your reasoning about the next step]
Action: [The tool you choose to use, if any from ONLY [{tools_name}]]
Action Input: [The input you provide to the tool]
Remember:
- Avoid using triple quotes (multi-line strings, docstrings) when providing multi line code.
- You have to provide all nessesary information in 'Action Input' for successfull next step.
- Provide Action Input in JSON format.
- MUST Begin each response with a "Thought" explaining your reasoning.
- If you need to use a tool, follow the thought with an "Action" (choosing from the available tools) and an "Action Input".
- After each action, the user will provide an "Observation" with the result.
- Continue this Thought/Action/Action Input/Observation sequence until you have enough information to answer the request.

When you have sufficient information, provide your final answer in one of these two formats:

If you can answer the request:

Thought: I can answer without using any tools
Answer: [Your answer here]
If you cannot answer the request:

Thought: I cannot answer with the tools I have
Answer: [Explanation of why you cannot answer]
Remember:
- Always start with a Thought.
- Never use markdown code markers around your response.
"""  # noqa: E501


REACT_BLOCK_INSTRUCTIONS_STRUCTURED_OUTPUT = """
If you have sufficient information to provide final answer, provide your final answer in one of these two formats:
If you can answer on request:
{{thought: [Why you can provide final answer],
action: finish
action_input: [Response for request]}}

If you can't answer on request:
{{thought: [Why you can not answer on request],
action: finish
answer: [Response for request]}}

Structure you responses in JSON format.
{{thought: [Your reasoning about the next step],
action: [The tool you choose to use, if any from ONLY [{tools_name}]],
action_input: [The input you provide to the tool]}}
"""  # noqa: E501


REACT_BLOCK_INSTRUCTIONS_FUNCTION_CALLING = """
You have to call appropriate functions.

Function descriptions
plan_next_action - function that should be called to use tools [{tools_name}]].
provide_final_answer - function that should be called when answer on initial request can be provided

"""  # noqa: E501


REACT_BLOCK_INSTRUCTIONS_NO_TOOLS = """
Always structure your responses in the following format:

Thought: [Your reasoning why you can not answer on initial question fully]
Observation: [Answer on initial question or part of it]
- Do not add information that is not connected to main request.
- MUST Begin each response with a "Thought" explaining your reasoning.
- After each action, the user will provide an "Observation" with the result.
- Continue this Thought/Action/Action Input/Observation sequence until you have enough information to answer the request.

When you have sufficient information, provide your final answer in one of these two formats:

If you can answer the request:

Thought: I can answer without using any tools
Answer: [Your answer here]
If you cannot answer the request:

Thought: I cannot answer with the tools I have
Answer: [Explanation of why you cannot answer]
Remember:
- Always start with a Thought.
- Never use markdown code markers around your response.
"""  # noqa: E501


REACT_BLOCK_OUTPUT_FORMAT = """
In your final answer do not use wording like `based on the information gathered or provided`.
Just provide a clear and concise answer.
"""  # noqa: E501

REACT_BLOCK_REQUEST = "User request: {input}"
REACT_BLOCK_CONTEXT = "Below is the conversation: {context}"


def function_calling_schema(tool_names):
    return [
        {
            "type": "function",
            "function": {
                "name": "plan_next_action",
                "description": "Provide next action and action input",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "thought": {
                            "type": "string",
                            "description": "Your reasoning about the next step.",
                        },
                        "action": {
                            "type": "string",
                            "enum": tool_names,
                            "description": "Next action to make.",
                        },
                        "action_input": {
                            "type": "string",
                            "description": "Input for chosen action.",
                        },
                    },
                    "required": ["thought", "action", "action_input"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "provide_final_answer",
                "description": "Function should be called when if you can answer the initial request",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "thought": {
                            "type": "string",
                            "description": "Your reasoning about why you can answer original question.",
                        },
                        "answer": {"type": "string", "description": "Answer on initial request."},
                    },
                    "required": ["thought", "answer"],
                },
            },
        },
    ]


def structured_output_schema(tool_names):
    return {
        "type": "json_schema",
        "json_schema": {
            "strict": True,
            "name": "plan_next_action",
            "schema": {
                "type": "object",
                "required": ["thought", "action", "action_input"],
                "properties": {
                    "thought": {
                        "type": "string",
                        "description": "Your reasoning about the next step.",
                    },
                    "action": {
                        "type": "string",
                        "description": f"Next action to make (choose from [{tool_names}, finish]).",
                    },
                    "action_input": {
                        "type": "string",
                        "description": "Input for chosen action.",
                    },
                },
                "additionalProperties": False,
            },
        },
    }


class ReActAgent(Agent):
    """Agent that uses the ReAct strategy for processing tasks by interacting with tools in a loop."""

    name: str = "React"
    max_loops: int = Field(default=15, ge=1)
    inference_mode: InferenceMode = InferenceMode.DEFAULT

    def parse_xml_content(self, text: str, tag: str) -> str:
        """Extract content from XML-like tags."""
        match = re.search(f"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        return match.group(1).strip() if match else ""

    def parse_xml_and_extract_info(self, text: str) -> dict[str, Any]:
        """Parse XML-like structure and extract action and action_input."""
        output_content = self.parse_xml_content(text, "output")
        action = self.parse_xml_content(output_content, "action")
        action_input_text = self.parse_xml_content(output_content, "action_input")

        try:
            action_input = json.loads(action_input_text)
        except json.JSONDecodeError:
            raise ActionParsingException(
                (
                    "Error: Could not parse action and action input."
                    "Please rewrite in the appropriate XML format with action_input as a valid dictionary."
                ),
                recoverable=True,
            )

        return action, action_input

    def extract_output_and_answer_xml(self, text: str) -> dict[str, str]:
        """Extract output and answer from XML-like structure."""
        output = self.parse_xml_content(text, "output")
        answer = self.parse_xml_content(text, "answer")

        logger.debug(f"Extracted output: {output}")
        logger.debug(f"Extracted answer: {answer}")

        return {"output": output, "answer": answer}

    def tracing_final(self, loop_num, final_answer, config, kwargs):
        self._intermediate_steps[loop_num]["final_answer"] = final_answer
        if self.streaming.enabled:
            self.run_on_node_execute_stream(
                config.callbacks,
                self._intermediate_steps[loop_num],
                **kwargs,
            )

    def tracing_intermediate(self, loop_num, formatted_prompt, llm_generated_output):
        self._intermediate_steps[loop_num] = AgentIntermediateStep(
            input_data={"prompt": formatted_prompt},
            model_observation=AgentIntermediateStepModelObservation(
                initial=llm_generated_output,
            ),
        ).model_dump()

    def _run_agent(self, config: RunnableConfig | None = None, **kwargs) -> str:
        """
        Executes the ReAct strategy by iterating through thought, action, and observation cycles.
        Args:
            config (RunnableConfig | None): Configuration for the agent run.
            **kwargs: Additional parameters for running the agent.
        Returns:
            str: Final answer provided by the agent.
        Raises:
            RuntimeError: If the maximum number of loops is reached without finding a final answer.
            Exception: If an error occurs during execution.
        """

        logger.info(f"Agent {self.name} - {self.id}: Running ReAct strategy")
        previous_responses = []

        for loop_num in range(self.max_loops):
            formatted_prompt = self.generate_prompt(
                user_request=kwargs.get("input", ""),
                tools_desc=self.tool_description,
                tools_name=self.tool_names,
                context="\n".join(previous_responses),
            )
            logger.info(f"Agent {self.name} - {self.id}: Loop {loop_num + 1} started.")

            logger.debug(f"Agent {self.name} - {self.id}: Loop {loop_num + 1}. Prompt:\n{formatted_prompt}")

            try:
                schema = {}
                match self.inference_mode:
                    case InferenceMode.FUNCTION_CALLING:
                        schema = function_calling_schema(self.tool_names.split(","))
                    case InferenceMode.STRUCTURED_OUTPUT:
                        schema = structured_output_schema(self.tool_names.split(","))

                # Execute the prompt using the LLM
                llm_result = self.llm.run(
                    input_data={},
                    config=config,
                    prompt=Prompt(
                        messages=[Message(role="user", content=formatted_prompt)]
                    ),
                    run_depends=self._run_depends,
                    schema=schema,
                    inference_mode=self.inference_mode,
                    **kwargs,
                )

                self._run_depends = [NodeDependency(node=self.llm).to_dict()]

                if llm_result.status != RunnableStatus.SUCCESS:
                    logger.error(
                        f"Agent {self.name} - {self.id}: Loop {loop_num + 1} LLM execution failed. "
                        f"Error output: {llm_result.output}"
                    )
                    previous_responses.append(llm_result.output["content"])
                    continue

                action, action_input = None, None
                llm_generated_output = ""
                match self.inference_mode:
                    case InferenceMode.DEFAULT:
                        llm_generated_output = llm_result.output["content"]
                        logger.debug(
                            f"Agent {self.name} - {self.id}:Loop {loop_num + 1}. "
                            f"RAW LLM output:n{llm_generated_output}"
                        )
                        self.tracing_intermediate(loop_num, formatted_prompt, llm_generated_output)
                        if "Answer:" in llm_generated_output:
                            final_answer = self._extract_final_answer(llm_generated_output)
                            self.tracing_final(loop_num, final_answer, config, kwargs)
                            return final_answer

                        action, action_input = self._parse_action(llm_generated_output)

                    case InferenceMode.FUNCTION_CALLING:
                        function_name = llm_result.output["tool_calls"][0]["function"]["name"].strip()
                        llm_generated_output_json = json.loads(
                            llm_result.output["tool_calls"][0]["function"]["arguments"]
                        )
                        llm_generated_output = json.dumps(llm_generated_output_json)
                        self.tracing_intermediate(loop_num, formatted_prompt, llm_generated_output)

                        if function_name == "provide_final_answer":
                            final_answer = llm_generated_output_json["answer"]
                            self.tracing_final(loop_num, final_answer, config, kwargs)
                            return final_answer

                        action, action_input = llm_generated_output_json["action"], {
                            "input": llm_generated_output_json["action_input"]
                        }
                        logger.debug(
                            f"Agent {self.name} - {self.id}:Loop {loop_num + 1}. "
                            f"RAW LLM output:n{llm_generated_output}"
                        )
                    case InferenceMode.STRUCTURED_OUTPUT:
                        llm_generated_output_json = json.loads(llm_result.output["content"])
                        action, action_input = llm_generated_output_json["action"], {
                            "input": llm_generated_output_json["action_input"]
                        }
                        llm_generated_output = json.dumps(llm_generated_output_json)
                        self.tracing_intermediate(loop_num, formatted_prompt, llm_generated_output)

                        if action == "finish":
                            final_answer = llm_generated_output_json["action_input"]
                            self.tracing_final(loop_num, final_answer, config, kwargs)
                            return final_answer

                    case InferenceMode.XML:
                        llm_generated_output = llm_result.output["content"]
                        self.tracing_intermediate(loop_num, formatted_prompt, llm_generated_output)
                        if "<answer>" in llm_generated_output:
                            final_answer = self._extract_final_answer_xml(llm_generated_output)
                            self.tracing_final(loop_num, final_answer, config, kwargs)
                            return final_answer
                        action, action_input = self.parse_xml_and_extract_info(llm_generated_output)

                if action:
                    logger.debug(f"Agent {self.name} - {self.id}:Loop {loop_num + 1}. Action:\n{action}")
                    logger.debug(f"Agent {self.name} - {self.id}:Loop {loop_num + 1}. Action Input:\n{action_input}")

                    if self.tools:
                        try:
                            tool = self._get_tool(action)
                            tool_result = self._run_tool(tool, action_input, config, **kwargs)

                            logger.debug(
                                f"Agent {self.name} - {self.id}:Loop {loop_num + 1}. Tool Result:\n{tool_result}"
                            )

                        except RecoverableAgentException as e:
                            tool_result = f"{type(e).__name__}: {e}"

                        llm_generated_output += f"\nObservation: {tool_result}\n"

                        self._intermediate_steps[loop_num]["model_observation"].update(
                            AgentIntermediateStepModelObservation(
                                tool_using=action,
                                tool_input=action_input,
                                tool_output=tool_result,
                                updated=llm_generated_output,
                            ).model_dump()
                        )

                        if self.streaming.enabled:
                            self.run_on_node_execute_stream(
                                config.callbacks,
                                self._intermediate_steps[loop_num],
                                **kwargs,
                            )
                previous_responses.append(llm_generated_output)

            except ActionParsingException as e:
                logger.error(f"Agent {self.name} - {self.id}:Loop {loop_num + 1}. failed with error: {str(e)}")
                previous_responses.append(f"{type(e).__name__}: {e}")
                continue
        logger.error(f"Agent {self.name} - {self.id}: Maximum number of loops reached.")
        raise AgentMaxLoopsReached(f"Agent {self.name} - {self.id}: Maximum number of loops reached.")

    def _extract_final_answer_xml(self, llm_output: str) -> str:
        """Extract the final answer from the LLM output."""
        final_answer = self.extract_output_and_answer_xml(llm_output)
        logger.info(f"Agent {self.name} - {self.id}: Final answer found: {final_answer['answer']}")
        return final_answer["answer"]

    def _init_prompt_blocks(self):
        """Initialize the prompt blocks required for the ReAct strategy."""
        super()._init_prompt_blocks()

        prompt_blocks = {
            "tools": REACT_BLOCK_TOOLS if self.tools else REACT_BLOCK_NO_TOOLS,
            "instructions": REACT_BLOCK_INSTRUCTIONS if self.tools else REACT_BLOCK_INSTRUCTIONS_NO_TOOLS,
            "output_format": REACT_BLOCK_OUTPUT_FORMAT,
            "request": REACT_BLOCK_REQUEST,
            "context": REACT_BLOCK_CONTEXT,
        }

        match self.inference_mode:
            case InferenceMode.FUNCTION_CALLING:
                prompt_blocks["instructions"] = REACT_BLOCK_INSTRUCTIONS_FUNCTION_CALLING
            case InferenceMode.STRUCTURED_OUTPUT:
                prompt_blocks["instructions"] = REACT_BLOCK_INSTRUCTIONS_STRUCTURED_OUTPUT
            case InferenceMode.DEFAULT:
                if not self.tools:
                    prompt_blocks["tools"] = REACT_BLOCK_NO_TOOLS
                    prompt_blocks["instructions"] = REACT_BLOCK_INSTRUCTIONS_NO_TOOLS
            case InferenceMode.XML:
                prompt_blocks["instructions"] = REACT_BLOCK_XML_INSTRUCTIONS

        self._prompt_blocks.update(prompt_blocks)