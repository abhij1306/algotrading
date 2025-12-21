"""
LLM Client - Unified interface for AI model providers
Supports: Groq (Llama 3.3), OpenAI (GPT-4), Anthropic (Claude)
"""
import os
from typing import Dict, Any, Optional
import json
import httpx
from dotenv import load_dotenv

load_dotenv()


class LLMClient:
    """Unified LLM client supporting multiple providers via LiteLLM"""
    
    def __init__(self, provider: str = "groq"):
        """
        Initialize LLM client
        
        Args:
           provider: "groq", "openai", "anthropic", or "cerebras"
        """
        self.provider = provider
        
        # Map provider to default models (can be overridden by env)
        if provider == "groq":
            self.model = os.getenv("LITELLM_MODEL", "groq/llama-3.1-70b-versatile")
        elif provider == "openai":
            self.model = os.getenv("LITELLM_MODEL", "gpt-4-turbo-preview")
        elif provider == "anthropic":
            self.model = os.getenv("LITELLM_MODEL", "claude-3-sonnet-20240229")
        elif provider == "cerebras":
            self.model = os.getenv("LITELLM_MODEL", "cerebras/llama3.1-70b")
        else:
             # Allow direct model pass-through if provider is actually a model name
             self.model = provider

        print(f"✅ LLM Client initialized: provider={provider}, model={self.model}")

    async def generate(
        self, 
        prompt: str, 
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        json_mode: bool = True
    ) -> str:
        """
        Generate LLM response using LiteLLM
        """
        if not system_prompt:
            system_prompt = "You are an expert quantitative trader and risk manager. Provide precise, data-driven analysis in JSON format."
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]

        try:
            from litellm import completion
            
            # Prepare kwargs
            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            # Call LiteLLM (it handles async if we use acompletion, but standard completion is sync. 
            # ideally we use acompletion for async, but let's stick to simple completion wrapped or just check if we need async)
            # Actually, for async we should use `acompletion`
            from litellm import acompletion
            
            response = await acompletion(**kwargs)
            
            return response.choices[0].message.content
            
        except Exception as e:
            error_msg = f"LLM generation failed (LiteLLM): {str(e)}"
            print(f"❌ {error_msg}")
            # Fallback or re-raise
            raise Exception(error_msg)
    
    async def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2000
    ) -> Dict[str, Any]:
        """
        Generate and parse JSON response
        
        Args:
            prompt: User prompt
            system_prompt: System prompt (optional)
            temperature: Sampling temperature
            max_tokens: Maximum tokens
            
        Returns:
            Parsed JSON dict
        """
        response = await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True
        )
        
        # Try to extract JSON from response
        try:
            # First try direct parsing
            return json.loads(response)
        except json.JSONDecodeError:
            # Try to find JSON in markdown code blocks
            import re
            
            # Try ```json ... ``` blocks
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group(1))
                except json.JSONDecodeError:
                    pass
            
            # Try ``` ... ``` blocks (without json marker)
            json_match = re.search(r'```\s*(\{.*?\})\s*```', response, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group(1))
                except json.JSONDecodeError:
                    pass
            
            # Try to find first complete JSON object (non-greedy)
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group(0))
                except json.JSONDecodeError:
                    pass
            
            # Last resort: try to find any {...} and parse it
            json_match = re.search(r'(\{.*\})', response, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
                # Try to find the end of the first complete JSON object
                brace_count = 0
                for i, char in enumerate(json_str):
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            try:
                                return json.loads(json_str[:i+1])
                            except json.JSONDecodeError:
                                break
            
            raise ValueError(f"Could not parse JSON from response: {response[:500]}...")


# Singleton instance
_llm_client = None

def get_llm_client(provider: str = "groq") -> LLMClient:
    """Get or create LLM client singleton"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient(provider=provider)
    return _llm_client
