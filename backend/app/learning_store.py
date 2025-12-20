"""
Learning Artifacts Store
Provides versioned storage for agent-generated insights and patterns.
"""
from sqlalchemy.orm import Session
from .database import LearningArtifact, Company
from .access_control import AccessControl, AgentType
from typing import Optional, List, Dict, Any

class LearningStore:
    def __init__(self, db: Session, agent_id: str):
        self.db = db
        self.agent_id = agent_id
        self.ac = AccessControl(agent_id)

    def save_artifact(self, 
                      artifact_type: str, 
                      content: Dict[str, Any], 
                      symbol: Optional[str] = None):
        """Save a new learning artifact with access control"""
        self.ac.enforce_write("learning_artifacts")
        
        company_id = None
        if symbol:
            company = self.db.query(Company).filter(Company.symbol == symbol).first()
            if company:
                company_id = company.id
        
        # Check if version exists to auto-increment
        latest = self.db.query(LearningArtifact).filter(
            LearningArtifact.agent_id == self.agent_id,
            LearningArtifact.artifact_type == artifact_type,
            LearningArtifact.company_id == company_id
        ).order_by(LearningArtifact.version.desc()).first()
        
        version = (latest.version + 1) if latest else 1
        
        new_artifact = LearningArtifact(
            company_id=company_id,
            agent_id=self.agent_id,
            artifact_type=artifact_type,
            content=content,
            version=version
        )
        
        self.db.add(new_artifact)
        self.db.commit()
        return new_artifact

    def get_artifacts(self, 
                      artifact_type: Optional[str] = None, 
                      symbol: Optional[str] = None) -> List[LearningArtifact]:
        """Fetch artifacts (READ is allowed for all authenticated agents)"""
        query = self.db.query(LearningArtifact)
        
        if artifact_type:
            query = query.filter(LearningArtifact.artifact_type == artifact_type)
        
        if symbol:
            company = self.db.query(Company).filter(Company.symbol == symbol).first()
            if company:
                query = query.filter(LearningArtifact.company_id == company.id)
                
        return query.order_by(LearningArtifact.created_at.desc()).all()
