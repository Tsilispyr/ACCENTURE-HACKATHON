"""Shared LLM client, constructed once and imported everywhere -- same
pattern used throughout the whole curriculum (day02-day04, docerz, day21).

load_dotenv() is called HERE, not left to whichever caller happens to import
this module first -- AzureChatOpenAI validates credential presence eagerly
at construction (not just on .invoke()), so this module must not depend on
import order to have .env already loaded. (Caught by graph.py's own test
suite: importing hackathon1.graph directly, without hackathon1.service
having run first, failed with "Missing credentials" until this call moved
here.)
"""

import os

from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI

load_dotenv()

llm = AzureChatOpenAI(model=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME"))
