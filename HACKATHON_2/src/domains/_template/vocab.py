"""TIME BUDGET: 10 minutes. What things are called.

Do this FIRST - every prompt and the planner depend on it, and it is the
cheapest file to get right.
"""

# The system prompt. Be specific about what it may and may not do, and what it
# must say when it does not know. A vague persona produces a vague assistant.
# Copy the shape from domains/sample_policy or domains/sample_ops.
PERSONA = """\
You are a <ROLE> assistant for <WHO>.

Answer ONLY from the extracts provided.
 - Cite the <SECTION / RECORD> behind every statement.
 - If the extracts do not answer the question, say exactly that and name what
   is missing. Do not fill the gap from memory.
 - Be concrete about <DEADLINES / THRESHOLDS / WHO IS RESPONSIBLE>.
 - Close with: "<ANY REQUIRED DISCLAIMER>"
"""

# Comma-separated entity names, handed to the planner so it writes steps in the
# domain's language instead of generic ones. Ten words is plenty.
GLOSSARY = "<entity>, <entity>, <entity>"
