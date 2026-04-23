"""Domain services — business logic on top of raw API clients.

Services are the unit of reasoning for the agent. Tools in agent/tools.py are
thin wrappers over service methods; the service is where validation,
aggregation, and orchestration happen.
"""
