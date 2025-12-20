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
    """Unified LLM client supporting multiple providers"""
    
    def __init__(self, provider: str = "groq"):
        """
        Initialize LLM client
        
        Args:
            provider: "groq", "openai", or "anthropic"
        """
        self.provider = provider
        
        if provider == "groq":
            self.api_key = os.getenv("GROQ_API_KEY")
            self.base_url = "https://api.groq.com/openai/v1"
            self.model = "llama-3.1-8b-instant"
        elif provider == "openai":
            self.api_key = os.getenv("OPENAI_API_KEY")
            self.base_url = "https://api.openai.com/v1"
            self.model = "gpt-4-turbo-preview"
        elif provider == "anthropic":
            self.api_key = os.getenv("ANTHROPIC_API_KEY")
            self.base_url = "https://api.anthropic.com/v1"
            self.model = "claude-3-sonnet-20240229"
        else:
            raise ValueError(f"Unsupported provider: {provider}")
        
        if not self.api_key:
            raise ValueError(f"API key not found for provider: {provider}. Please set {provider.upper()}_API_KEY in .env")
        
        print(f"✅ LLM Client initialized: provider={provider}, model={self.model}, api_key={'*' * 10}{self.api_key[-4:]}")
    
    async def generate(
        self, 
        prompt: str, 
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        json_mode: bool = True
    ) -> str:
        """
        Generate LLM response
        
        Args:
            prompt: User prompt
            system_prompt: System prompt (optional)
            temperature: Sampling temperature (0-1)
            max_tokens: Maximum tokens to generate
            json_mode: Force JSON output
            
        Returns:
            Generated text response
        """
        if not system_prompt:
            system_prompt = "You are an expert quantitative trader and risk manager. Provide precise, data-driven analysis in JSON format."
        
        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                if self.provider in ["groq", "openai"]:
                    # OpenAI-compatible API
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    }
                    
                    payload = {
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt}
                        ],
                        "temperature": temperature,
                        "max_tokens": max_tokens
                    }
                    
                    if json_mode and self.provider == "openai":
                        payload["response_format"] = {"type": "json_object"}
                    
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload
                    )
                    response.raise_for_status()
                    data = response.json()
                    return data["choices"][0]["message"]["content"]
                
                elif self.provider == "anthropic":
                    # Anthropic API
                    headers = {
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json"
                    }
                    
                    payload = {
                        "model": self.model,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "system": system_prompt,
                        "messages": [
                            {"role": "user", "content": prompt}
                        ]
                    }
                    
                    response = await client.post(
                        f"{self.base_url}/messages",
                        headers=headers,
                        json=payload
                    )
                    response.raise_for_status()
                    data = response.json()
                    return data["content"][0]["text"]
        
        except httpx.HTTPStatusError as e:
            error_msg = f"LLM API error: {e.response.status_code} - {e.response.text}"
            print(f"❌ {error_msg}")
            raise Exception(error_msg)
        except Exception as e:
            error_msg = f"LLM generation failed: {str(e)}"
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()
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
