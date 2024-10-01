from dynamiq import Workflow
from dynamiq.connections import Anthropic as AnthropicConnection
from dynamiq.connections import OpenAI as OpenAIConnection
from dynamiq.connections import ScaleSerp, ZenRows
from dynamiq.flows import Flow
from dynamiq.nodes.agents.base import Agent
from dynamiq.nodes.agents.orchestrators.adaptive import AdaptiveOrchestrator
from dynamiq.nodes.agents.orchestrators.adaptive_manager import AdaptiveAgentManager
from dynamiq.nodes.agents.react import ReActAgent
from dynamiq.nodes.llms.anthropic import Anthropic
from dynamiq.nodes.llms.openai import OpenAI
from dynamiq.nodes.tools.scale_serp import ScaleSerpTool
from dynamiq.nodes.tools.zenrows import ZenRowsTool
from dynamiq.utils.logger import logger
from examples.trip_planner.prompts import generate_customer_prompt, generate_simple_customer_prompt

# Please use your own file path
OUTPUT_FILE_PATH = "city_guide_gpt.md"


def choose_provider(model_type, model_name):
    if model_type == "gpt":
        _connection = OpenAIConnection()
        _llm = OpenAI(
            connection=_connection,
            model=model_name,
            temperature=0.1,
            max_tokens=4000,
        )
    elif model_type == "claude":
        _connection = AnthropicConnection()
        _llm = Anthropic(
            connection=_connection,
            model=model_name,
            temperature=0.1,
            max_tokens=4000,
        )
    else:
        raise ValueError("Invalid model provider specified.")
    return _llm


def inference(input_data: dict, model_type="gpt", model_name="gpt-4o-mini", use_advanced_prompt=False) -> dict:
    llm_agent = choose_provider(model_type, model_name)
    search_connection = ScaleSerp()
    zenrows_connection = ZenRows()
    tool_search = ScaleSerpTool(connection=search_connection)
    tool_scrape = ZenRowsTool(connection=zenrows_connection)

    # Create agents
    agent_selection_city = ReActAgent(
        name="City Selection Expert",
        role="An expert in analyzing travel data to pick ideal destinations",
        goal=(
            "help select the best city for a trip based on specific criteria such as weather patterns, seasonal events, and travel costs."  # noqa: E501
        ),
        llm=llm_agent,
        tools=[tool_search],
        max_loops=10,
    )

    agent_city_guide = ReActAgent(
        name="City Guide Expert",
        role="An expert in gathering information about a city",
        goal=(
            "compile an in-depth guide for someone traveling to a city, including key attractions, local customs, special events, and daily activity recommendations."  # noqa: E501
        ),
        llm=llm_agent,
        tools=[tool_search, tool_scrape],
        max_loops=10,
    )

    agent_writer = Agent(
        name="City Guide Writer",
        role="An expert in creating detailed travel guides",
        goal="write a detailed travel guide for a city, including key attractions, local customs, special events, and daily activity recommendations.",  # noqa: E501
        llm=llm_agent,
    )
    agent_manager = AdaptiveAgentManager(
        llm=llm_agent,
    )

    # Create a adaptive orchestrator
    adaptive_orchestrator = AdaptiveOrchestrator(
        manager=agent_manager,
        agents=[agent_city_guide, agent_selection_city, agent_writer],
    )
    # Create a workflow
    workflow = Workflow(flow=Flow(nodes=[adaptive_orchestrator]))

    if use_advanced_prompt:
        user_prompt = generate_customer_prompt(input_data)
    else:
        user_prompt = generate_simple_customer_prompt(input_data)

    result = workflow.run(
        input_data={
            "input": user_prompt,
        }
    )
    logger.info("Workflow completed")
    content = result.output[adaptive_orchestrator.id]
    return content


if __name__ == "__main__":
    user_location = input("Enter your location: ")
    user_cities = input("Enter cities you want to visit: ")
    user_dates = input("Enter dates: ")
    user_interests = input("Enter your interests: ")
    input_data = {
        "location": user_location,
        "cities": user_cities,
        "dates": user_dates,
        "interests": user_interests,
    }
    content = inference(input_data)["output"]["content"]
    print(content)
    with open(OUTPUT_FILE_PATH, "w") as f:
        f.write(content)