import json,time,uuid
from typing import Any,Type
from pydantic import BaseModel
from infrastructure.config.settings import get_settings

class AIRequest(BaseModel):
    task:str
    input_text:str
    prompt:str|None=None
    context:dict[str,Any]|None=None
    temperature:float=0.2
    max_tokens:int=2000
    response_schema:Type[BaseModel]|None=None

class AIResponse(BaseModel):
    request_id:str
    result:str
    model_used:str
    tokens_used:int|None=None
    latency_ms:int

class AIGateway:
    def __init__(self,api_key,model=None):
        self.model=model or get_settings().gemini_model
        from google import genai
        from google.genai import types
        self.types=types
        self.client=genai.Client(api_key=api_key)
        self.async_client=self.client.aio

    async def _call(self,contents,config=None,model=None):
        last=None
        import asyncio
        for attempt in range(get_settings().ai_retry_attempts):
            try:
                return await self.async_client.models.generate_content(model=model or self.model,contents=contents,config=config)
            except Exception as exc:
                last=exc
                if attempt == get_settings().ai_retry_attempts-1: raise
                await asyncio.sleep(2**attempt)
        raise last

    async def execute(self,request:AIRequest):
        started=time.perf_counter(); text=request.input_text
        if request.context: text += "\n\nCONTEXT:\n"+json.dumps(request.context,default=str)
        if request.prompt: text=request.prompt+"\n\n"+text
        cfg=self.types.GenerateContentConfig(temperature=request.temperature,max_output_tokens=request.max_tokens)
        if request.response_schema:
            cfg=self.types.GenerateContentConfig(temperature=request.temperature,max_output_tokens=request.max_tokens,response_mime_type="application/json",response_schema=request.response_schema)
        response=await self._call(text,cfg)
        usage=getattr(response,"usage_metadata",None)
        tokens=getattr(usage,"total_token_count",None) if usage else None
        return AIResponse(request_id=str(uuid.uuid4()),result=response.text or "",model_used=self.model,tokens_used=tokens,latency_ms=int((time.perf_counter()-started)*1000))

    async def summarize(self,text,context=None):
        return (await self.execute(AIRequest(task="summarization",input_text=text,context=context,prompt="Summarize accurately with evidence. Never invent facts."))).result

    async def classify(self,text,categories):
        return (await self.execute(AIRequest(task="classification",input_text=text,context={"categories":categories},prompt="Return the best category only."))).result.strip()

    async def extract(self,text,schema):
        r=await self.execute(AIRequest(task="extraction",input_text=text,response_schema=schema,prompt="Extract only facts present in the source.")); return json.loads(r.result)

    async def analyze_file(self,data:bytes,mime_type:str,prompt:str):
        import tempfile
        suffix = ".bin"
        if "/" in mime_type:
            suffix = "." + mime_type.split("/",1)[1].split(";",1)[0]
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data); path=tmp.name
        try:
            upload=await self.async_client.files.upload(file=path,config=self.types.UploadFileConfig(mime_type=mime_type))
            response=await self._call([upload,prompt])
            return response.text or ""
        finally:
            import os
            try: os.unlink(path)
            except OSError: pass

    async def transcribe_audio(self,data:bytes,mime_type:str,prompt="Transcribe the audio faithfully and preserve speaker changes when identifiable."):
        return await self.analyze_file(data,mime_type,prompt)

    async def aclose(self):
        await self.async_client.aclose()
        self.client.close()

_gateway=None

def close_ai_gateway_sync():
    global _gateway
    _gateway=None

async def close_ai_gateway():
    global _gateway
    if _gateway is not None:
        await _gateway.aclose()
        _gateway=None

def get_ai_gateway(api_key=None):
    global _gateway
    if _gateway is None:
        key=api_key or get_settings().gemini_api_key
        if not key: raise RuntimeError("GEMINI_API_KEY is not configured")
        _gateway=AIGateway(key)
    return _gateway
