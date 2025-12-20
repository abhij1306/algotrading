"""
Central Access Control for Agents
Defines READ/WRITE permissions and enforces boundaries.
"""
from typing import List, Optional
from enum import Enum

class AgentType(Enum):
    DATA_AGENT = "data_agent"
    STRATEGY_AGENT = "strategy_agent"
    RISK_AGENT = "risk_agent"
    UI_AGENT = "ui_agent"

class AccessControl:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        
        # Define allowed write tables for each agent
        self.permissions = {
            AgentType.DATA_AGENT.value: ["companies", "historical_prices", "corporate_actions"],
            AgentType.STRATEGY_AGENT.value: ["learning_artifacts", "backtests", "strategies"],
            AgentType.RISK_AGENT.value: ["computed_risk_metrics", "user_portfolios"],
            AgentType.UI_AGENT.value: ["ui_preferences"] # placeholder
        }

    def can_write(self, table_name: str) -> bool:
        """Check if current agent can write to specific table"""
        allowed = self.permissions.get(self.agent_id, [])
        return table_name in allowed

    def enforce_write(self, table_name: str):
        """Raise error if write is not allowed"""
        if not self.can_write(table_name):
            raise PermissionError(f"Agent {self.agent_id} is not allowed to write to table {table_name}")

def get_access_control(agent_id: str) -> AccessControl:
    return AccessControl(agent_id)
