# from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
# from agent.tools import tool_descriptions


# # System prompt with clear instructions about task completion
# SYSTEM_PROMPT_THINK = f"""You are a AI Software Architecture Consulting to Human Software Engineer.
# you get set of tool that the software engineer can use to complete the task.
# Think step by step and consult the engineer what should be his next step.
# In your analysis output a 3-6 sentences express your reasoning from you vast experience.
# You cannot call the tools directly your output should be a paragraph explaining what should the engineer do and what is the purpose.
# don't ask questions or permission output only paragraph 3-6 sentence of analysis instruct what should be done and why.
# you may get a messages from human software engineer that follow your guidelines you should take it into account in your next analysis.
# Available tools:
# {tool_descriptions}

# You must reflect your analysis as you reflect the reasoning to yourself use terms like I would, I think, I must do...
# Don't assume any previous knowledge about the codebase you are working with analyse it as you see it for the first time.
# """

# SYSTEM_PROMPT_ACT = f"""You are a Software Engineering Agent. you will get thought of senior AI Software Architecture Consulting.
# You must follow the instructions and use the tools to complete the task based on the analysis of the software architecture consulting.
# don't provide your own reasoning instead follow the reasoning of the AI Software Architecture thought.
# """


# PROMPT_THINK = ChatPromptTemplate([
#     ("system", SYSTEM_PROMPT_THINK),
#     MessagesPlaceholder(variable_name="messages"),  # The thoughts and actions history. the first message is ("human", "{input}"),  # The user input task
# ])

# PROMPT_ACT = ChatPromptTemplate([
#     ("system", SYSTEM_PROMPT_ACT),
#     MessagesPlaceholder(variable_name="messages"),  # The thoughts and actions history. the first message is ("human", "{input}"),  # The user input task
# ])

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

# Robust import for tool_descriptions (works whether the package exports it or not)
try:
    from agent.tools import tool_descriptions  # prefer package export
except Exception:
    from agent.tools.search import search_tools
    from agent.tools.codemap import codemap_tools
    from agent.tools.agents_md import agents_md_tools

    def _tools_to_str(tools: list) -> str:
        return "\n---\n".join(
            f"Tool Name: {t.name}\nTool Description: {getattr(t, 'description', '')}" for t in tools
        )

    tool_descriptions = _tools_to_str(
        search_tools
        + codemap_tools
        + agents_md_tools
    )

# THINK — advisory only; no tool calls
SYSTEM_PROMPT_THINK = (
    "You are an AI Software Architecture Consultant advising a human Software Engineer.\n"
    "You see a list of tools the engineer can use to complete the task, but you will not call tools yourself.\n"
    "Think step by step and recommend the single best next action the engineer should take and why.\n"
    "Write exactly one paragraph of 3–6 sentences. Do not ask questions or seek permission.\n"
    "Use self-reflective language (e.g., “I would…”, “I think…”, “I must…”). "
    "Assume no prior knowledge of the codebase beyond what is provided.\n"
    "\nAvailable tools:\n"
    f"{tool_descriptions}\n"
)
# (No-CoT variant if needed later: “Write a concise 2–4 sentence plan. Do not reveal your internal chain-of-thought.”)

SYSTEM_PROMPT_ACT = (
    "You are a Software Engineering Agent. You will receive the architect’s analysis (THINK) as context.\n"
    "Follow that analysis exactly and use the available tools to complete the task. "
    "Do not add your own independent reasoning beyond executing the plan. "
    "When a tool is required, call it; otherwise, make a concise status update or finish.\n"
)

# Choose ONE of these patterns for THINK:

# A) If you pass `input=...` AND also pass a history list under `messages`:
PROMPT_THINK = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT_THINK),
    MessagesPlaceholder("messages"),   # prior turns, if any
    ("human", "{input}"),              # the new user task
])

# B) If you already append the human message into `messages`, use this instead:
# PROMPT_THINK = ChatPromptTemplate.from_messages([
#     ("system", SYSTEM_PROMPT_THINK),
#     MessagesPlaceholder("messages"),
# ])

# ACT — gets the full running thread (including THINK output)
PROMPT_ACT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT_ACT),
    MessagesPlaceholder("messages"),   # should include the THINK assistant message before ACT runs
])
