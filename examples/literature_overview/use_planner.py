"""
This script sets up and runs a multi-agent system using the dynamiq framework
to generate a literature overview based on user input. It utilizes various
LLM models, tools, and agents to research and write content.
"""

from dotenv import load_dotenv

from dynamiq import Workflow
from dynamiq.connections import Anthropic as AnthropicConnection
from dynamiq.connections import OpenAI as OpenAIConnection
from dynamiq.connections import ScaleSerp, ZenRows
from dynamiq.flows import Flow
from dynamiq.nodes.agents.base import Agent
from dynamiq.nodes.agents.orchestrators import LinearOrchestrator
from dynamiq.nodes.agents.orchestrators.linear_manager import LinearAgentManager
from dynamiq.nodes.agents.react import ReActAgent
from dynamiq.nodes.llms.anthropic import Anthropic
from dynamiq.nodes.llms.openai import OpenAI
from dynamiq.nodes.tools.scale_serp import ScaleSerpTool
from dynamiq.utils.logger import logger
from examples.tools.scraper import ScraperSummarizerTool

# Constants
GPT_MODEL = "gpt-4o"
CLAUDE_MODEL = "claude-3-5-sonnet-20240620"
# Please use your own file path
OUTPUT_FILE_PATH = "article_gpt.md"


def initialize_llm_models(model_type: str, model_name: str) -> tuple[OpenAI | Anthropic, OpenAI | Anthropic]:
    """
    Initialize and return LLM models based on the specified provider.

    Args:
        model_type (str): The model provider to use. Either "gpt" or "claude". Defaults to "gpt".
        model_name (str): The name of the model to use. Defaults to "gpt-4o".

    Returns:
        Tuple[Union[OpenAI, Anthropic], Union[OpenAI, Anthropic]]: A tuple containing two LLM instances:
            - The first for the ReActAgent
            - The second for general use

    Raises:
        ValueError: If an invalid model provider is specified.
    """
    if model_type == "gpt":
        connection = OpenAIConnection()
        llm_react_agent = OpenAI(
            connection=connection,
            model=model_name,
            temperature=0.5,
            max_tokens=4000,
            stop=["Observation:", "\nObservation:", "\n\tObservation:"],
        )
        llm = OpenAI(
            connection=connection,
            model=model_name,
            temperature=0.1,
            max_tokens=4000,
        )
    elif model_type == "claude":
        connection = AnthropicConnection()
        llm_react_agent = Anthropic(
            connection=connection,
            model=model_name,
            temperature=0.5,
            max_tokens=4000,
            stop=["Observation:", "\nObservation:"],
        )
        llm = Anthropic(
            connection=connection,
            model=model_name,
            temperature=0.1,
            max_tokens=4000,
        )
    else:
        raise ValueError(f"Invalid model provider: {model_type}")

    return llm_react_agent, llm


def inference(user_prompt: str, model_type="gpt", model_name="gpt-4o-mini") -> dict:
    """
    Set up and run a multi-agent workflow to generate a literature overview.

    This function initializes LLM models, creates necessary tools and agents,
    sets up a workflow, and executes it based on a user-provided prompt.
    The resulting content is printed to the console and saved to a file.
    """
    # Load environment variables
    load_dotenv()

    # Initialize LLM models and connections
    llm_react_agent, llm_agent = initialize_llm_models(model_type, model_name)
    serp_connection = ScaleSerp()
    zenrows_connection = ZenRows()

    # Create tools
    tool_search = ScaleSerpTool(connection=serp_connection)
    tool_scrape_summarizer = ScraperSummarizerTool(
        connection=zenrows_connection, llm=llm_agent
    )

    # Create agents
    agent_researcher = ReActAgent(
        name="Research Analyst",
        llm=llm_react_agent,
        tools=[tool_search, tool_scrape_summarizer],
        role="the Senior Research Analyst, that specializes in finding latest and most actual information",
        goal="to find the most relevant information regarding to the requested topic and provide to user",
        max_loops=8,
        function_calling=True,
    )

    agent_writer = Agent(
        name="Writer and Editor",
        llm=llm_agent,
        role="the Senior Writer and Editor, that specializes in creating high-quality content",
        goal="to create a high-quality content based on the information provided by the Research Analyst",
    )

    agent_manager = LinearAgentManager(llm=llm_agent)

    # Set up linear orchestrator
    linear_orchestrator = LinearOrchestrator(
        manager=agent_manager,
        agents=[agent_researcher, agent_writer],
        final_summarizer=True,
        saving_file=True,
    )

    # Set up workflow
    workflow = Workflow(flow=Flow(nodes=[linear_orchestrator]))

    # Run workflow and save results
    try:
        result = workflow.run(input_data={"input": user_prompt})
        logger.info("Workflow completed")

        content = result.output[linear_orchestrator.id]
        return content
    except Exception as e:
        logger.error(f"An error occurred during workflow execution: {str(e)}")


if __name__ == "__main__":
    user_prompt = "comprehensive study of the Turkish shooter in 2024 in Paris olympics,write a report for me"
    content = inference(user_prompt)["output"]["content"]
    print(content)

    with open(OUTPUT_FILE_PATH, "w") as f:
        f.write(content)
    logger.info(f"Results saved to {OUTPUT_FILE_PATH}")